"""
extract_art.py — Tách art từ ảnh mockup áo bằng rembg
======================================================
Input:
  - crop_coords.csv  : listing_id, x, y, w, h  (output của annotate_crops.py)
  - heyetsy_image_urls.csv : listing_id, image_1, shop_name, title, ...

Pipeline:
  1. Đọc crop_coords.csv → danh sách listing + tọa độ
  2. Join với heyetsy_image_urls.csv để lấy image_1 URL
  3. Download ảnh → crop theo tọa độ → rembg remove background
  4. Lưu PNG transparent → extracted_art/{listing_id}_art.png
  5. Lưu log kết quả → extract_log.csv

Cài đặt:
  pip install "rembg[cpu]" pillow requests pandas
"""

import os
import sys
import time
import signal
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from io import BytesIO

import requests
import pandas as pd
from PIL import Image

try:
    from rembg import remove, new_session
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"; WARN = "\033[93m"; CKPT = "\033[92m"
    ERROR = "\033[91m"; TIME = "\033[96m"; DONE = "\033[92m"; RESET = "\033[0m"

    @staticmethod
    def tag(color, label): return f"{color}[{label}]{C.RESET}"

def ts():    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))
def info(m): print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {m}")
def warn(m): print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {m}")
def ckpt(m): print(f"{ts()} {C.tag(C.CKPT,  'CKPT')}  {m}")
def done(m): print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {m}")
def err(m):  print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {m}")


# ── Config ────────────────────────────────────────────────────────────────────
CROP_COORDS_CSV  = "crop_coords.csv"
IMAGE_URLS_CSV   = "heyetsy_image_urls.csv"
OUTPUT_DIR       = Path("extracted_art")
PREVIEW_DIR      = Path("extracted_art/previews")
EXTRACT_LOG      = "extract_log.csv"
CHECKPOINT_EVERY = 10   # flush log mỗi N listings

REMBG_MODEL = "isnet-general-use"   # tốt nhất cho art/logo

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── Setup ─────────────────────────────────────────────────────────────────────
def setup_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


# ── Load inputs ───────────────────────────────────────────────────────────────
def load_crop_coords(path: str = CROP_COORDS_CSV) -> pd.DataFrame:
    """
    Đọc crop_coords.csv (output của annotate_crops.py).
    Cột cần: listing_id, x, y, w, h
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path} — chạy annotate_crops.py trước.")
        
    df = pd.read_csv(path, dtype=str).fillna("")
    required = {"listing_id", "x", "y", "w", "h"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"crop_coords.csv thiếu cột: {missing}")
    # Convert tọa độ sang int
    for col in ["x", "y", "w", "h"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    # Bỏ dòng có tọa độ không hợp lệ (w=0 hoặc h=0)
    invalid = df[(df["w"] == 0) | (df["h"] == 0)]
    if len(invalid):
        warn(f"Bỏ {len(invalid)} dòng có tọa độ không hợp lệ (w=0 hoặc h=0).")
        df = df[(df["w"] > 0) & (df["h"] > 0)]
    info(f"Loaded {len(df)} crop coords từ {path}")
    return df

def load_image_urls(path: str = IMAGE_URLS_CSV) -> pd.DataFrame:
    """
    Đọc heyetsy_image_urls.csv để lấy image_1 URL + metadata.
    Cột cần: listing_id, image_1
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path}")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "listing_id" not in df.columns or "image_1" not in df.columns:
        raise ValueError(f"{path} cần có cột 'listing_id' và 'image_1'")
    info(f"Loaded {len(df)} rows từ {path}")
    return df

def build_work_list(
    coords_df: pd.DataFrame,
    urls_df:   pd.DataFrame,
    skip_done: bool = True,
) -> List[Dict]:
    """
    Join crop_coords với image_urls theo listing_id.
    Bỏ qua listing đã có output nếu skip_done=True.
    Trả về list dict: {listing_id, x, y, w, h, image_url, shop_name, title}
    """
    # Chỉ giữ các cột cần thiết từ urls_df
    keep_cols = ["listing_id", "image_1"] + \
                [c for c in ["shop_name", "title"] if c in urls_df.columns]
    urls_slim = urls_df[keep_cols].copy()
    urls_slim = urls_slim[urls_slim["image_1"] != ""]

    merged = coords_df.merge(urls_slim, on="listing_id", how="left")

    # Bỏ dòng không có image_url
    no_url = merged[merged["image_1"].isna() | (merged["image_1"] == "")]
    if len(no_url):
        warn(f"{len(no_url)} listings không có image_1 URL — bỏ qua.")
        merged = merged[merged["image_1"].notna() & (merged["image_1"] != "")]

    work = merged.to_dict("records")

    if skip_done:
        before = len(work)
        work   = [r for r in work
                  if not (OUTPUT_DIR / f"{r['listing_id']}_art.png").exists()]
        skipped = before - len(work)
        if skipped:
            info(f"Bỏ qua {skipped} listings đã có output PNG.")

    info(f"Cần xử lý: {len(work)} listings")
    return work


# ── Download ──────────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[Image.Image]:
    """Download URL → PIL Image RGBA."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        err(f"Download lỗi: {e}")
        return None


# ── Crop ──────────────────────────────────────────────────────────────────────
def crop_image(img: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    """Crop PIL Image theo tọa độ (x, y, w, h), clamp trong bounds."""
    W, H = img.size
    x1 = max(0, x);        y1 = max(0, y)
    x2 = min(W, x + w);    y2 = min(H, y + h)
    return img.crop((x1, y1, x2, y2))


# ── rembg ─────────────────────────────────────────────────────────────────────
def remove_background(img: Image.Image, session) -> Image.Image:
    """rembg remove background. Input/output: PIL Image RGBA."""
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    result_bytes = remove(buf.read(), session=session)
    return Image.open(BytesIO(result_bytes)).convert("RGBA")


# ── Log ───────────────────────────────────────────────────────────────────────
def flush_log(log: List[Dict], path: str = EXTRACT_LOG):
    """Atomic write log."""
    if not log:
        return
    tmp = path + ".tmp"
    pd.DataFrame(log).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Log flush {len(log)} records → {path}")

def setup_signal(log: List[Dict]):
    def _handler(sig, frame):
        from signal import SIGINT
        print()
        warn("Ctrl+C — flush log rồi thoát...")
        flush_log(log)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── Main pipeline ─────────────────────────────────────────────────────────────
def extract_art(
    crop_csv:  str  = CROP_COORDS_CSV,
    image_csv: str  = IMAGE_URLS_CSV,
    skip_done: bool = True,
    save_preview: bool = True,
):
    """
    Entry point — batch extract art từ crop_coords.csv.
    """
    if not REMBG_AVAILABLE:
        err("rembg chưa cài. Chạy: pip install \"rembg[cpu]\"")
        sys.exit(1)

    setup_dirs()

    # Load inputs + join
    coords_df = load_crop_coords(crop_csv)
    urls_df   = load_image_urls(image_csv)
    work      = build_work_list(coords_df, urls_df, skip_done=skip_done)

    if not work:
        done("Không có gì để xử lý.")
        return pd.DataFrame()

    # Init rembg session (download model lần đầu)
    info(f"Khởi tạo rembg session (model: {REMBG_MODEL})...")
    session = new_session(REMBG_MODEL)
    done("rembg sẵn sàng.")

    log: List[Dict] = []
    setup_signal(log)

    total = len(work)
    items_since_flush = 0

    for i, row in enumerate(work, 1):
        lid       = row["listing_id"]
        x, y, w, h = int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])
        image_url = row["image_1"]
        shop      = row.get("shop_name", "")
        title     = str(row.get("title", ""))[:50]
        out_path  = OUTPUT_DIR / f"{lid}_art.png"

        print(f"\n{'─'*55}")
        info(f"[{i}/{total}] {lid}  shop={shop}")
        info(f"  Title: {title}")
        info(f"  Coords: x={x} y={y} w={w} h={h}")

        # Download
        img = download_image(image_url)
        if img is None:
            log.append({"listing_id": lid, "status": "download_failed",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        # Validate coords trong bounds ảnh
        W_img, H_img = img.size
        if x >= W_img or y >= H_img:
            err(f"  Tọa độ vượt bounds ảnh ({W_img}x{H_img}) — skip")
            log.append({"listing_id": lid, "status": "invalid_coords",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        # Crop
        cropped = crop_image(img, x, y, w, h)

        # Lưu preview
        if save_preview:
            prev_path = PREVIEW_DIR / f"{lid}_crop_preview.png"
            cropped.save(prev_path)

        # Remove background
        info(f"  rembg xử lý...")
        t0 = time.time()
        try:
            result  = remove_background(cropped, session)
            elapsed = round(time.time() - t0, 1)
        except Exception as e:
            err(f"  rembg lỗi: {e}")
            log.append({"listing_id": lid, "status": "rembg_failed",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        # Lưu output PNG
        result.save(out_path, format="PNG")
        done(f"  Saved ({elapsed}s) → {out_path.name}")

        log.append({
            "listing_id": lid,
            "shop_name":  shop,
            "title":      title,
            "status":     "done",
            "output":     str(out_path),
            "elapsed_s":  elapsed,
        })
        items_since_flush += 1

        # Flush log định kỳ
        if items_since_flush >= CHECKPOINT_EVERY:
            flush_log(log)
            items_since_flush = 0

    # Final flush
    flush_log(log)

    # Summary
    df_log  = pd.DataFrame(log)
    success = (df_log["status"] == "done").sum() if not df_log.empty else 0
    failed  = len(log) - success
    avg_t   = df_log[df_log["status"] == "done"]["elapsed_s"].astype(float).mean() \
              if success else 0

    print(f"\n{'═'*55}")
    done(f"Tổng: {total}  ✓ {success}  ✗ {failed}")
    done(f"Thời gian TB/ảnh: {avg_t:.1f}s")
    done(f"Output: {OUTPUT_DIR}/")
    done(f"Log: {EXTRACT_LOG}")

    return df_log


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch extract art bằng rembg từ crop_coords.csv")
    parser.add_argument("--crop-csv",     default=CROP_COORDS_CSV, help=f"Crop coords CSV (default: {CROP_COORDS_CSV})")
    parser.add_argument("--image-csv",    default=IMAGE_URLS_CSV,  help=f"Image URLs CSV (default: {IMAGE_URLS_CSV})")
    parser.add_argument("--redo",         action="store_true",      help="Redo cả listing đã có output PNG")
    parser.add_argument("--no-preview",   action="store_true",      help="Không lưu preview crop")
    args = parser.parse_args()

    df = extract_art(
        crop_csv     = args.crop_csv,
        image_csv    = args.image_csv,
        skip_done    = not args.redo,
        save_preview = not args.no_preview,
    )

    if not df.empty:
        print(df[["listing_id", "status", "elapsed_s"]].to_string(index=False))
