"""
download_images.py
──────────────────
Bước 1: Download ảnh của N listing từ heyetsy_image_urls.csv về folder.

Cấu trúc folder output:
  images/
    PhotoOnShirt/
      1696869276/
        image_1.jpg
        image_2.jpg
        ...
      1737951991/
        image_1.jpg
        ...

Sau khi chạy xong → chạy upload_to_trello.py để upload lên Trello.
"""

import os
import re
import sys
import time
import json
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

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
INPUT_CSV        = "heyetsy_image_urls.csv"
SHOP_FILTER      = "PhotoOnShirt"
LISTING_LIMIT    = 100
OUTPUT_DIR       = "images"          # thư mục gốc chứa ảnh
CHECKPOINT_FILE  = "download_checkpoint.json"   # lưu listing_id đã download xong

DOWNLOAD_TIMEOUT = 15    # giây timeout mỗi ảnh
DELAY_BETWEEN    = 0.2   # giây giữa mỗi request download


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
            info(f"Loaded {len(df)} rows từ {csv_path} (encoding={enc})")
            return df
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Không đọc được {csv_path}")

def extract_images(row: pd.Series) -> List[str]:
    return [
        row[c] for c in sorted(
            [c for c in row.index if re.match(r"^image_\d+$", c)],
            key=lambda x: int(x.split("_")[1])
        ) if row[c]
    ]


# ── Checkpoint ────────────────────────────────────────────────────────────────
def load_checkpoint() -> set:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_checkpoint(done_ids: set) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(done_ids), f, ensure_ascii=False)


# ── Download ──────────────────────────────────────────────────────────────────
def download_image(url: str, save_path: str) -> bool:
    """Download 1 ảnh từ URL → lưu vào save_path. Trả về True nếu thành công."""
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        warn(f"    Download thất bại ({url[-50:]}): {e}")
        return False

def get_ext_from_url(url: str) -> str:
    """Lấy extension từ URL, mặc định .jpg."""
    filename = url.split("/")[-1].split("?")[0]
    _, ext = os.path.splitext(filename)
    return ext if ext else ".jpg"


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    df = load_csv(INPUT_CSV)

    # Lọc shop + có ảnh + giới hạn số listing
    mask = (df["shop_name"] == SHOP_FILTER)
    if "image_1" in df.columns:
        mask &= (df["image_1"] != "")
    df_shop = df[mask].head(LISTING_LIMIT).reset_index(drop=True)

    info(f"Shop: {SHOP_FILTER} — {len(df_shop)} listings sẽ download")

    if df_shop.empty:
        err(f"Không tìm thấy listing nào của {SHOP_FILTER}.")
        sys.exit(1)

    # Resume checkpoint
    done_ids = load_checkpoint()
    remaining = df_shop[~df_shop["listing_id"].isin(done_ids)]
    info(f"Đã download: {len(done_ids)} | Còn lại: {len(remaining)}")

    total_ok   = 0
    total_fail = 0

    for i, (_, row) in enumerate(remaining.iterrows(), 1):
        lid        = row.get("listing_id", "")
        shop_name  = row.get("shop_name", SHOP_FILTER)
        image_urls = extract_images(row)

        if not lid or not image_urls:
            skip(f"[{i}/{len(remaining)}] Bỏ qua — không có lid hoặc ảnh")
            continue

        info(f"[{i}/{len(remaining)}] {lid} — {len(image_urls)} ảnh")

        # Tạo folder: images/{shop_name}/{listing_id}/
        folder = os.path.join(OUTPUT_DIR, shop_name, lid)
        os.makedirs(folder, exist_ok=True)

        ok = fail = 0
        for j, url in enumerate(image_urls, 1):
            ext       = get_ext_from_url(url)
            filename  = f"image_{j}{ext}"
            save_path = os.path.join(folder, filename)

            # Skip nếu file đã tồn tại
            if os.path.exists(save_path):
                skip(f"  [{j}/{len(image_urls)}] {filename} đã có — skip")
                ok += 1
                continue

            success = download_image(url, save_path)
            if success:
                size_kb = os.path.getsize(save_path) // 1024
                done(f"  [{j}/{len(image_urls)}] {filename} ({size_kb}KB)")
                ok += 1
            else:
                fail += 1

            time.sleep(DELAY_BETWEEN)

        total_ok   += ok
        total_fail += fail
        done(f"  → {ok}/{len(image_urls)} OK | folder: {folder}")

        # Đánh dấu listing đã xong (kể cả khi có ảnh fail — để không re-download)
        done_ids.add(lid)
        save_checkpoint(done_ids)

    # Summary
    print(f"\n{'─'*55}")
    done("Download hoàn tất!")
    info(f"  Ảnh download OK   : {total_ok}")
    info(f"  Ảnh thất bại      : {total_fail}")
    info(f"  Folder output     : {os.path.abspath(OUTPUT_DIR)}")
    info(f"  Checkpoint        : {CHECKPOINT_FILE}")
    print(f"{'─'*55}")
    print(f"\n→ Chạy tiếp: python upload_to_trello.py\n")


if __name__ == "__main__":
    run()
