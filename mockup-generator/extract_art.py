"""
extract_art.py
──────────────
Tách art từ ảnh áo thun → transparent background PNG.

Flow:
  1. Đọc image_1 URL từ heyetsy_image_urls.csv
  2. Download ảnh + hiển thị kích thước để user xác định tọa độ crop
  3. User nhập x, y, w, h để crop vùng art
  4. Gửi vùng crop lên Bria AI RMBG-2.0 (qua Replicate) để remove background
  5. Lưu kết quả PNG transparent vào thư mục output

Yêu cầu:
  pip install requests pillow replicate
  REPLICATE_API_TOKEN = "r8_..."  (https://replicate.com/account/api-tokens)
"""

import os
import sys
import io
import time
import requests
import replicate
import pandas as pd
from PIL import Image
from datetime import datetime
from typing import Optional, Tuple

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"
    WARN  = "\033[93m"
    DONE  = "\033[92m"
    ERROR = "\033[91m"
    TIME  = "\033[96m"
    INPUT = "\033[93m"
    RESET = "\033[0m"

    @staticmethod
    def tag(color, label):
        return f"{color}[{label}]{C.RESET}"

def ts():
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {msg}")
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {msg}")
def sep():      print("─" * 60)


# ── Config ────────────────────────────────────────────────────────────────────
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")

INPUT_CSV    = "heyetsy_image_urls.csv"
OUTPUT_DIR   = "extracted_art"
PREVIEW_DIR  = "extracted_art/preview"  # lưu ảnh crop trước khi remove bg

# Replicate model: Bria RMBG-2.0
REPLICATE_MODEL = "bria-ai/rmbg-2.0"

# Số listing test (None = tất cả)
TEST_LIMIT = 5


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(
                csv_path, dtype=str, sep=None,
                engine="python", encoding=enc
            ).fillna("")
            info(f"Loaded {len(df)} rows từ {csv_path} (encoding={enc})")
            return df
        except UnicodeDecodeError:
            if enc == "latin-1":
                raise
            continue
    raise RuntimeError(f"Không đọc được {csv_path}")


# ── Image helpers ─────────────────────────────────────────────────────────────
def download_image(url: str, timeout: int = 15) -> Optional[Image.Image]:
    """Download ảnh từ URL → PIL Image."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as e:
        err(f"Download thất bại ({url[:60]}): {e}")
        return None

def save_image(img: Image.Image, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)

def crop_image(img: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    """Crop vùng (x, y, x+w, y+h) từ ảnh."""
    W, H = img.size
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(W, x + w)
    y2 = min(H, y + h)
    return img.crop((x1, y1, x2, y2))


# ── Crop input ────────────────────────────────────────────────────────────────
def prompt_crop(img: Image.Image, listing_id: str) -> Optional[Tuple[int,int,int,int]]:
    """
    Hiển thị kích thước ảnh và hỏi user nhập tọa độ crop.
    Trả về (x, y, w, h) hoặc None nếu skip.
    """
    W, H = img.size
    sep()
    info(f"Listing: {listing_id}")
    info(f"Kích thước ảnh: {W} x {H} px")
    print(f"\n  Tọa độ (0,0) = góc trên trái")
    print(f"  x = cách trái, y = cách trên, w = chiều rộng, h = chiều cao")
    print(f"  VD: x=100 y=150 w=400 h=400  (crop vùng art giữa áo)\n")

    raw = input(
        f"  {C.tag(C.INPUT, 'INPUT')} Nhập x y w h (cách nhau dấu cách) "
        f"hoặc 's' để skip: "
    ).strip()

    if raw.lower() == "s":
        warn(f"  Skip listing {listing_id}")
        return None

    parts = raw.split()
    if len(parts) != 4:
        err("  Cần đúng 4 số: x y w h")
        return None

    try:
        x, y, w, h = [int(p) for p in parts]
        if w <= 0 or h <= 0:
            err("  w và h phải > 0")
            return None
        return x, y, w, h
    except ValueError:
        err("  Nhập không hợp lệ — cần số nguyên")
        return None


# ── Replicate: remove background ─────────────────────────────────────────────
def remove_background_replicate(img: Image.Image) -> Optional[Image.Image]:
    """
    Gửi ảnh lên Bria RMBG-2.0 qua Replicate để remove background.
    Trả về PIL Image với transparent background (RGBA).
    """
    if not REPLICATE_API_TOKEN:
        err("Chưa set REPLICATE_API_TOKEN!")
        return None

    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    # Convert PIL Image → bytes để gửi lên API
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)

    try:
        info("  Gửi lên Replicate (Bria RMBG-2.0)...")
        output = replicate.run(
            REPLICATE_MODEL,
            input={"image": buf}
        )

        # Output là URL của ảnh đã remove bg
        if isinstance(output, str):
            result_url = output
        elif hasattr(output, "url"):
            result_url = output.url
        else:
            result_url = str(output)

        info(f"  Download kết quả từ Replicate...")
        resp = requests.get(result_url, timeout=30)
        resp.raise_for_status()
        result_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        return result_img

    except Exception as e:
        err(f"  Replicate thất bại: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def run(
    csv_path:   str = INPUT_CSV,
    output_dir: str = OUTPUT_DIR,
    limit:      int = TEST_LIMIT,
) -> None:
    if not REPLICATE_API_TOKEN:
        err("Chưa set REPLICATE_API_TOKEN!")
        err("Lấy token tại: https://replicate.com/account/api-tokens")
        err("Sau đó set: REPLICATE_API_TOKEN=r8_... trong environment hoặc trong file này")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(PREVIEW_DIR, exist_ok=True)

    df = load_csv(csv_path)

    # Lọc dòng có image_1
    if "image_1" in df.columns:
        df = df[df["image_1"] != ""].reset_index(drop=True)

    if limit:
        df = df.head(limit)

    info(f"Sẽ xử lý {len(df)} listings (limit={limit})")

    success = 0
    skipped = 0
    failed  = 0

    for i, (_, row) in enumerate(df.iterrows(), 1):
        lid      = row.get("listing_id", f"listing_{i}")
        img_url  = row.get("image_1", "")
        shop     = row.get("shop_name", "")

        if not img_url:
            warn(f"[{i}/{len(df)}] {lid} — không có image_1, skip")
            skipped += 1
            continue

        sep()
        info(f"[{i}/{len(df)}] listing={lid}  shop={shop}")
        info(f"  URL: {img_url[:80]}")

        # Download ảnh
        img = download_image(img_url)
        if img is None:
            failed += 1
            continue

        # User nhập tọa độ crop
        crop_coords = prompt_crop(img, lid)
        if crop_coords is None:
            skipped += 1
            continue

        x, y, w, h = crop_coords

        # Crop
        cropped = crop_image(img, x, y, w, h)
        info(f"  Crop: ({x},{y}) {w}x{h} → {cropped.size[0]}x{cropped.size[1]} px")

        # Lưu preview crop
        preview_path = os.path.join(PREVIEW_DIR, f"{lid}_crop.png")
        save_image(cropped, preview_path)
        info(f"  Preview saved → {preview_path}")

        # Xác nhận trước khi gửi API
        confirm = input(
            f"  {C.tag(C.INPUT, 'INPUT')} Gửi lên Replicate để remove background? (Y/n): "
        ).strip().lower()

        if confirm == "n":
            warn("  Bỏ qua bước remove background.")
            skipped += 1
            continue

        # Remove background
        result = remove_background_replicate(cropped)
        if result is None:
            failed += 1
            continue

        # Lưu kết quả
        out_path = os.path.join(output_dir, f"{lid}_art.png")
        save_image(result, out_path)
        done(f"  Saved → {out_path}")
        success += 1

        # Nghỉ ngắn giữa các API call
        time.sleep(0.5)

    # Summary
    sep()
    done("Hoàn tất!")
    info(f"  Thành công  : {success}")
    info(f"  Skipped     : {skipped}")
    info(f"  Thất bại    : {failed}")
    info(f"  Output dir  : {os.path.abspath(output_dir)}")
    sep()


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()