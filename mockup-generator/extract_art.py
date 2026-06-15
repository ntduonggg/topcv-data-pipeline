"""
extract_art.py — Tách art từ ảnh mockup áo
==========================================
Pipeline:
  1. Đọc heyetsy_image_urls.csv → lấy image_1 URL + metadata
  2. Download ảnh về local (temp)
  3. Hiển thị ảnh + kích thước → user nhập tọa độ crop (x, y, w, h)
  4. Crop vùng art
  5. rembg remove background vùng đã crop
  6. Lưu PNG transparent → extracted_art/{listing_id}_art.png
  7. Lưu log crop tọa độ → crop_coords.csv (để batch sau)

Cài đặt:
  pip install rembg pillow requests pandas
  hoặc: uv add rembg pillow requests pandas
"""

import os
import re
import sys
import time
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, List

import requests
import pandas as pd
from PIL import Image
from io import BytesIO

try:
    from rembg import remove, new_session
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False
    print("[WARN] rembg chưa cài — chạy: pip install rembg")

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"; WARN = "\033[93m"; CKPT = "\033[92m"
    ERROR = "\033[91m"; TIME = "\033[96m"; DONE = "\033[92m"; RESET = "\033[0m"

    @staticmethod
    def tag(color, label): return f"{color}[{label}]{C.RESET}"

def ts():    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))
def info(m): print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {m}")
def warn(m): print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {m}")
def done(m): print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {m}")
def err(m):  print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {m}")


# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV   = "heyetsy_image_urls.csv"
OUTPUT_DIR  = Path("extracted_art")
PREVIEW_DIR = Path("extracted_art/previews")   # crop preview trước khi remove bg
CROP_LOG    = "crop_coords.csv"                # lưu tọa độ để batch sau
LIMIT       = 5                                # số listing test (None = tất cả)

# rembg model — isnet-general-use tốt nhất cho art/logo
REMBG_MODEL = "isnet-general-use"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


# ── Setup ─────────────────────────────────────────────────────────────────────
def setup_dirs():
    OUTPUT_DIR.mkdir(exist_ok=True)
    PREVIEW_DIR.mkdir(exist_ok=True)


# ── Download ảnh ──────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[Image.Image]:
    """Download ảnh từ URL → PIL Image."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        err(f"Download lỗi: {e}")
        return None


# ── Crop helpers ──────────────────────────────────────────────────────────────
def prompt_crop(img: Image.Image, listing_id: str) -> Optional[Tuple[int,int,int,int]]:
    """
    Hiển thị kích thước ảnh, user nhập tọa độ crop.
    Format: x y w h (cách nhau bằng space)
    Nhập 's' để skip listing này.
    Nhập 'q' để quit.
    """
    W, H = img.size
    print(f"\n  Ảnh: {W} x {H} px  (listing_id: {listing_id})")
    print(f"  Nhập tọa độ crop: x y w h  (góc trên trái + width height)")
    print(f"  VD: 100 80 300 300  |  's' = skip  |  'q' = quit")

    while True:
        raw = input("  > ").strip().lower()
        if raw == "q":
            sys.exit(0)
        if raw == "s":
            return None
        parts = raw.split()
        if len(parts) == 4:
            try:
                x, y, w, h = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                # Validate trong bounds ảnh
                if x < 0 or y < 0 or x + w > W or y + h > H:
                    print(f"  [WARN] Tọa độ vượt ra ngoài ảnh ({W}x{H}), nhập lại.")
                    continue
                return x, y, w, h
            except ValueError:
                pass
        print("  [WARN] Format sai. VD: 100 80 300 300")

def crop_image(img: Image.Image, coords: Tuple[int,int,int,int]) -> Image.Image:
    """Crop ảnh theo (x, y, w, h) → PIL Image."""
    x, y, w, h = coords
    return img.crop((x, y, x + w, y + h))


# ── rembg remove background ───────────────────────────────────────────────────
def remove_background(img: Image.Image, session) -> Image.Image:
    """
    Dùng rembg remove background.
    Input: PIL Image (vùng đã crop)
    Output: PIL Image RGBA (transparent background)
    """
    # Convert sang bytes để rembg xử lý
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    result_bytes = remove(buf.read(), session=session)
    return Image.open(BytesIO(result_bytes)).convert("RGBA")


# ── Crop log ──────────────────────────────────────────────────────────────────
def load_crop_log() -> Dict[str, Tuple[int,int,int,int]]:
    """Load tọa độ crop đã lưu từ lần trước."""
    if not os.path.exists(CROP_LOG):
        return {}
    coords = {}
    try:
        df = pd.read_csv(CROP_LOG, dtype=str).fillna("")
        for _, row in df.iterrows():
            lid = row.get("listing_id", "")
            if lid:
                coords[lid] = (
                    int(row.get("x", 0)), int(row.get("y", 0)),
                    int(row.get("w", 0)), int(row.get("h", 0)),
                )
    except Exception as e:
        warn(f"Không đọc được crop log: {e}")
    return coords

def save_crop_log(coords_map: Dict[str, Tuple[int,int,int,int]]):
    """Lưu tọa độ crop vào CSV."""
    rows = [
        {"listing_id": lid, "x": c[0], "y": c[1], "w": c[2], "h": c[3]}
        for lid, c in coords_map.items()
    ]
    pd.DataFrame(rows).to_csv(CROP_LOG, index=False, encoding="utf-8-sig")
    done(f"Crop coords saved → {CROP_LOG}")


# ── Load input CSV ────────────────────────────────────────────────────────────
def load_listings(csv_path: str, limit: Optional[int] = None) -> List[Dict]:
    """
    Đọc heyetsy_image_urls.csv.
    Lấy: listing_id, shop_name, title, image_1 (URL ảnh đầu tiên).
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    required = {"listing_id", "image_1"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV thiếu cột: {missing}")

    # Chỉ lấy dòng có image_1
    df = df[df["image_1"] != ""]

    if limit:
        df = df.head(limit)

    records = df.to_dict("records")
    info(f"Loaded {len(records)} listings từ {csv_path}")
    return records


# ── Main pipeline ─────────────────────────────────────────────────────────────
def extract_art(
    csv_path:   str  = INPUT_CSV,
    output_dir: Path = OUTPUT_DIR,
    limit:      Optional[int] = LIMIT,
    batch_mode: bool = False,   # True: dùng tọa độ từ crop_log, không hỏi
):
    """
    Entry point.
    batch_mode=False: hỏi tọa độ từng listing (interactive)
    batch_mode=True:  dùng tọa độ đã lưu trong crop_coords.csv
    """
    if not REMBG_AVAILABLE:
        err("rembg chưa cài. Chạy: pip install rembg")
        sys.exit(1)

    setup_dirs()

    listings   = load_listings(csv_path, limit)
    crop_log   = load_crop_log()
    session    = new_session(REMBG_MODEL)
    coords_map = dict(crop_log)   # copy để update

    info(f"rembg model: {REMBG_MODEL}")
    info(f"Mode: {'batch' if batch_mode else 'interactive'}")
    if batch_mode:
        info(f"Loaded {len(crop_log)} tọa độ từ {CROP_LOG}")

    results = []

    for i, row in enumerate(listings, 1):
        lid       = row["listing_id"]
        shop      = row.get("shop_name", "")
        title     = row.get("title", "")[:50]
        image_url = row["image_1"]
        out_path  = output_dir / f"{lid}_art.png"

        print(f"\n{'─'*55}")
        info(f"[{i}/{len(listings)}] {lid}  shop={shop}")
        info(f"  Title: {title}")

        # Skip nếu đã có output
        if out_path.exists():
            warn(f"  Đã có {out_path.name} — skip")
            results.append({"listing_id": lid, "status": "skipped", "output": str(out_path)})
            continue

        # Download ảnh
        info(f"  Download: {image_url[:60]}...")
        img = download_image(image_url)
        if img is None:
            results.append({"listing_id": lid, "status": "download_failed", "output": ""})
            continue

        # Lấy tọa độ crop
        if batch_mode and lid in coords_map:
            coords = coords_map[lid]
            info(f"  Dùng tọa độ đã lưu: {coords}")
        elif batch_mode:
            warn(f"  Không có tọa độ cho {lid} trong batch mode — skip")
            results.append({"listing_id": lid, "status": "no_coords", "output": ""})
            continue
        else:
            # Interactive: hiển thị + hỏi user
            coords = prompt_crop(img, lid)
            if coords is None:
                warn(f"  Skipped bởi user.")
                results.append({"listing_id": lid, "status": "skipped_by_user", "output": ""})
                continue
            coords_map[lid] = coords

        # Crop
        cropped = crop_image(img, coords)

        # Lưu preview crop để kiểm tra trước khi remove bg
        preview_path = PREVIEW_DIR / f"{lid}_crop_preview.png"
        cropped.save(preview_path)
        info(f"  Preview crop: {preview_path}")

        # Xác nhận trước khi remove bg
        if not batch_mode:
            confirm = input("  Remove background? (Enter=yes / 's'=skip): ").strip().lower()
            if confirm == "s":
                results.append({"listing_id": lid, "status": "skipped_rembg", "output": ""})
                continue

        # Remove background
        info(f"  rembg đang xử lý...")
        t0     = time.time()
        result = remove_background(cropped, session)
        elapsed = time.time() - t0
        done(f"  Remove bg xong ({elapsed:.1f}s) → {out_path.name}")

        # Lưu output
        result.save(out_path, format="PNG")
        results.append({"listing_id": lid, "status": "done", "output": str(out_path)})

    # Lưu crop log
    save_crop_log(coords_map)

    # Summary
    print(f"\n{'═'*55}")
    total   = len(results)
    success = sum(1 for r in results if r["status"] == "done")
    skipped = sum(1 for r in results if "skip" in r["status"])
    failed  = total - success - skipped
    done(f"Tổng: {total}  ✓ {success}  ⟳ {skipped}  ✗ {failed}")
    done(f"Output: {output_dir}/")

    return pd.DataFrame(results)


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Tách art từ ảnh mockup áo bằng rembg")
    parser.add_argument("--csv",    default=INPUT_CSV,  help="CSV input (default: heyetsy_image_urls.csv)")
    parser.add_argument("--limit",  type=int, default=LIMIT, help="Số listing test (default: 5)")
    parser.add_argument("--batch",  action="store_true", help="Batch mode: dùng tọa độ từ crop_coords.csv")
    parser.add_argument("--all",    action="store_true", help="Chạy tất cả (bỏ qua LIMIT)")
    args = parser.parse_args()

    limit = None if args.all else args.limit

    df = extract_art(
        csv_path   = args.csv,
        limit      = limit,
        batch_mode = args.batch,
    )

    print(df.to_string(index=False))