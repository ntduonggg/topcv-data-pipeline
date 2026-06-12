"""
merge_heyetsy_exports.py
────────────────────────
Đọc tất cả file CSV export từ HeyEtsy bulk tool,
so sánh listing_id với heyetsy_image_urls.csv,
bổ sung image URLs vào các dòng còn thiếu.

Cách dùng:
    python merge_heyetsy_exports.py
    python merge_heyetsy_exports.py --export-dir "C:/Users/admin1/Downloads"
    python merge_heyetsy_exports.py --files "file1.csv" "file2.csv"
"""

import os
import re
import glob
import argparse
from datetime import datetime
from typing import Dict, List

import pandas as pd

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"
    WARN  = "\033[93m"
    CKPT  = "\033[92m"
    ERROR = "\033[91m"
    TIME  = "\033[96m"
    DONE  = "\033[92m"
    SKIP  = "\033[90m"
    RESET = "\033[0m"

    @staticmethod
    def tag(color, label):
        return f"{color}[{label}]{C.RESET}"

def ts():
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {msg}")
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {msg}")
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,  'CKPT')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {msg}")
def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,  'SKIP')}  {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_EXPORT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
DEFAULT_OUTPUT_CSV = "heyetsy_image_urls.csv"
EXPORT_PATTERN     = "heyetsy*listings*images*.csv"

# Regex extract URL fullxfull từ raw text
URL_REGEX = re.compile(
    r"https://i\.etsystatic\.com/\S+?il_fullxfull\.\S+?\.jpg"
)


# ── find_export_files ─────────────────────────────────────────────────────────
def find_export_files(export_dir: str) -> List[str]:
    pattern = os.path.join(export_dir, EXPORT_PATTERN)
    files = glob.glob(pattern)
    if not files:
        warn(f"Không tìm thấy file theo pattern '{EXPORT_PATTERN}' — thử tất cả CSV...")
        files = glob.glob(os.path.join(export_dir, "*.csv"))
    files = sorted(files, key=os.path.getmtime)
    info(f"Tìm thấy {len(files)} file export trong {export_dir}")
    for f in files:
        info(f"  → {os.path.basename(f)}")
    return files


# ── parse_export_file ─────────────────────────────────────────────────────────
def parse_export_file(csv_path: str) -> Dict[str, List[str]]:
    """
    Đọc từng dòng RAW — không dùng pd.read_csv.

    Lý do: file export HeyEtsy dùng dấu phẩy làm separator
    nhưng bản thân URL cũng chứa dấu phẩy → pandas đọc sai cột,
    bỏ sót các dòng có nhiều ảnh hơn dòng đầu tiên.

    Cách xử lý:
      - Mỗi dòng: tách token đầu tiên (listing_id) tại dấu phẩy đầu tiên
      - Phần còn lại: dùng regex extract toàn bộ URL il_fullxfull
    """
    result: Dict[str, List[str]] = {}

    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        err(f"Không đọc được {os.path.basename(csv_path)}: {e}")
        return result

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Tìm dấu phẩy đầu tiên để tách listing_id
        comma_pos = line.find(",")
        if comma_pos == -1:
            continue

        first = line[:comma_pos].strip()

        # Nếu cột đầu là etsy URL thì extract id từ đó
        m = re.search(r"/listing/(\d+)", first)
        listing_id = m.group(1) if m else first

        # Bỏ qua nếu không phải số (header, dòng lỗi, ...)
        if not listing_id.isdigit():
            continue

        # Extract toàn bộ URL fullxfull từ phần còn lại của dòng
        rest = line[comma_pos + 1:]
        raw_urls = URL_REGEX.findall(rest)

        # Dedup, giữ thứ tự
        seen: set = set()
        urls: List[str] = []
        for url in raw_urls:
            url = url.rstrip(".,")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        if not urls:
            continue

        # Merge nếu listing_id xuất hiện nhiều lần trong file
        if listing_id in result:
            for url in urls:
                if url not in set(result[listing_id]):
                    result[listing_id].append(url)
        else:
            result[listing_id] = urls

    return result


# ── merge_all_exports ─────────────────────────────────────────────────────────
def merge_all_exports(export_files: List[str]) -> Dict[str, List[str]]:
    """
    Merge tất cả file export → 1 dict {listing_id → [image URLs]}.
    File sau ghi đè file trước nếu có nhiều ảnh hơn.
    """
    merged: Dict[str, List[str]] = {}
    total = 0

    for f in export_files:
        data = parse_export_file(f)
        info(f"  {os.path.basename(f)}: {len(data)} listings")
        total += len(data)
        for lid, urls in data.items():
            if lid not in merged or len(urls) > len(merged[lid]):
                merged[lid] = urls

    info(f"Tổng parse: {total} entries → {len(merged)} listing_id unique")
    return merged


# ── update_output_csv ─────────────────────────────────────────────────────────
def update_output_csv(output_csv: str, image_map: Dict[str, List[str]]) -> None:
    if not os.path.exists(output_csv):
        err(f"Không tìm thấy: {output_csv}")
        return

    df = pd.read_csv(output_csv, dtype=str).fillna("")
    info(f"Loaded {len(df)} rows từ {output_csv}")

    if "listing_id" not in df.columns:
        err(f"Thiếu cột 'listing_id' trong {output_csv}")
        return

    if "image_1" not in df.columns:
        df["image_1"] = ""

    updated = skipped = notfound = 0

    for listing_id, urls in image_map.items():
        if not urls:
            continue
        matches = df[df["listing_id"] == listing_id].index
        if matches.empty:
            notfound += 1
            continue
        idx = matches[0]
        if df.at[idx, "image_1"] != "":
            skipped += 1
            continue

        # Xoá image cũ
        for c in df.columns:
            if re.match(r"^image_\d+$", c):
                df.at[idx, c] = ""

        # Ghi URLs mới
        for n, url in enumerate(urls, 1):
            col = f"image_{n}"
            if col not in df.columns:
                df[col] = ""
            df.at[idx, col] = url

        updated += 1

    # Sắp xếp cột: fixed + image_N
    fixed  = [c for c in ["shop_name", "listing_id", "title", "tags"] if c in df.columns]
    images = sorted(
        [c for c in df.columns if re.match(r"^image_\d+$", c)],
        key=lambda x: int(x.split("_")[1])
    )
    df = df[fixed + images].fillna("")

    tmp = output_csv + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, output_csv)

    still_missing = (df["image_1"] == "").sum()
    print(f"\n{'─'*50}")
    done(f"Saved → {output_csv}")
    info(f"  Đã bổ sung ảnh     : {updated}")
    info(f"  Skip (đã có ảnh)   : {skipped}")
    info(f"  Không tìm thấy ID  : {notfound}")
    info(f"  Vẫn còn thiếu ảnh  : {still_missing} / {len(df)}")
    print(f"{'─'*50}\n")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Merge HeyEtsy export CSVs vào heyetsy_image_urls.csv"
    )
    parser.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR,
                        help=f"Thư mục chứa CSV export (default: Downloads)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help=f"File output cần bổ sung (default: {DEFAULT_OUTPUT_CSV})")
    parser.add_argument("--files", nargs="+",
                        help="Chỉ định file cụ thể thay vì quét thư mục")
    args = parser.parse_args()

    print(f"\n{'═'*55}")
    info("HeyEtsy Export Merger")
    print(f"{'═'*55}\n")

    export_files = args.files if args.files else find_export_files(args.export_dir)

    if not export_files:
        err("Không có file nào để xử lý — thoát.")
        return

    print()
    info("Parsing export files...")
    image_map = merge_all_exports(export_files)

    if not image_map:
        err("Không parse được dữ liệu từ các file export.")
        return

    print()
    info(f"Bổ sung vào {args.output}...")
    update_output_csv(args.output, image_map)


if __name__ == "__main__":
    main()