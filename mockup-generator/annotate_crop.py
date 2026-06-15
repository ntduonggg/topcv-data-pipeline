"""
annotate_crops.py — Tool annotation tọa độ crop art từ ảnh mockup
==================================================================
Flow:
  1. Đọc heyetsy_image_urls.csv → lấy image_1 URL + metadata
  2. Download ảnh → hiện lên cửa sổ OpenCV
  3. User kéo chuột chọn vùng art → ENTER xác nhận
  4. Tự động lưu (x, y, w, h) vào crop_coords.csv
  5. Sang ảnh tiếp theo

Phím tắt:
  ENTER / SPACE  → xác nhận vùng đã chọn
  C              → chọn lại (reset ROI)
  S              → skip listing này
  Z              → quay lại listing trước (undo)
  P              → dùng tọa độ listing trước (copy)
  Q              → quit, lưu tất cả đã chọn

Cài đặt:
  pip install opencv-python pillow requests pandas
"""

import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, List

import cv2
import numpy as np
import requests
import pandas as pd
from PIL import Image
from io import BytesIO

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"; WARN = "\033[93m"; CKPT = "\033[92m"
    ERROR = "\033[91m"; TIME = "\033[96m"; DONE = "\033[92m"
    RESET = "\033[0m"

    @staticmethod
    def tag(color, label): return f"{color}[{label}]{C.RESET}"

def ts():    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))
def info(m): print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {m}")
def warn(m): print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {m}")
def done(m): print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {m}")
def err(m):  print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {m}")


# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV    = "heyetsy_image_urls.csv"
CROP_LOG     = "crop_coords.csv"
PREVIEW_DIR  = Path("extracted_art/previews")
MAX_DISPLAY  = 900    # px — resize ảnh nếu lớn hơn để vừa màn hình

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

WINDOW_NAME = "Annotate Art Region  |  ENTER=OK  C=Reset  S=Skip  Z=Undo  P=Copy prev  Q=Quit"


# ── Download ──────────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[np.ndarray]:
    """Download ảnh từ URL → numpy array (BGR cho OpenCV)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        img_pil = Image.open(BytesIO(r.content)).convert("RGB")
        return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    except Exception as e:
        err(f"Download lỗi: {e}")
        return None


# ── Resize helper (giữ aspect ratio) ─────────────────────────────────────────
def resize_for_display(img: np.ndarray, max_size: int = MAX_DISPLAY) -> Tuple[np.ndarray, float]:
    """
    Resize ảnh để vừa màn hình.
    Trả về (img_resized, scale) — scale dùng để convert tọa độ ngược lại.
    """
    H, W = img.shape[:2]
    scale = min(max_size / W, max_size / H, 1.0)
    if scale < 1.0:
        new_w = int(W * scale)
        new_h = int(H * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return img, scale


# ── Crop log ──────────────────────────────────────────────────────────────────
def load_crop_log(path: str = CROP_LOG) -> Dict[str, Tuple[int,int,int,int]]:
    """Load tọa độ đã lưu."""
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        coords = {}
        for _, row in df.iterrows():
            lid = row.get("listing_id", "")
            if lid:
                coords[lid] = (
                    int(row.get("x", 0)), int(row.get("y", 0)),
                    int(row.get("w", 0)), int(row.get("h", 0)),
                )
        return coords
    except Exception as e:
        warn(f"Không đọc được crop log: {e}")
        return {}

def save_crop_log(coords_map: Dict[str, Tuple[int,int,int,int]], path: str = CROP_LOG):
    """Atomic write: ghi .tmp → rename."""
    rows = [
        {"listing_id": lid, "x": c[0], "y": c[1], "w": c[2], "h": c[3]}
        for lid, c in coords_map.items()
    ]
    tmp = path + ".tmp"
    pd.DataFrame(rows).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    done(f"Saved {len(rows)} coords → {path}")


# ── Load listings ─────────────────────────────────────────────────────────────
def load_listings(csv_path: str, limit: Optional[int]) -> List[Dict]:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    if "listing_id" not in df.columns or "image_1" not in df.columns:
        raise ValueError("CSV cần có cột 'listing_id' và 'image_1'")
    df = df[df["image_1"] != ""]
    if limit:
        df = df.head(limit)
    records = df.to_dict("records")
    info(f"Loaded {len(records)} listings từ {csv_path}")
    return records


# ── Save preview crop ─────────────────────────────────────────────────────────
def save_preview(img_bgr: np.ndarray, coords: Tuple[int,int,int,int], listing_id: str):
    """Lưu ảnh crop preview để kiểm tra sau."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    x, y, w, h = coords
    crop = img_bgr[y:y+h, x:x+w]
    path = PREVIEW_DIR / f"{listing_id}_crop_preview.png"
    cv2.imwrite(str(path), crop)


# ── Draw overlay ──────────────────────────────────────────────────────────────
def draw_info_overlay(img: np.ndarray, text_lines: List[str]) -> np.ndarray:
    """Vẽ thông tin lên góc trên trái ảnh."""
    overlay = img.copy()
    y0 = 20
    for line in text_lines:
        cv2.putText(overlay, line, (10, y0),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(overlay, line, (10, y0),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        y0 += 22
    return overlay


# ── Core annotation ───────────────────────────────────────────────────────────
def annotate_one(
    img_bgr:   np.ndarray,
    listing_id: str,
    shop_name:  str,
    title:      str,
    index:      int,
    total:      int,
    prev_coords: Optional[Tuple[int,int,int,int]] = None,
) -> Optional[Tuple[int,int,int,int]]:
    """
    Hiện ảnh, user kéo chọn vùng art.
    Trả về (x, y, w, h) tọa độ thực (đã scale ngược nếu resize).
    Trả về None nếu skip.
    Trả về 'undo' (string) nếu user nhấn Z.
    Trả về 'quit' nếu user nhấn Q.
    """
    H_orig, W_orig = img_bgr.shape[:2]
    display, scale = resize_for_display(img_bgr)

    # Overlay thông tin
    lines = [
        f"[{index}/{total}] {listing_id}  shop={shop_name}",
        f"Title: {title[:55]}",
        f"Size: {W_orig}x{H_orig}px  (display scale: {scale:.2f})",
    ]
    if prev_coords:
        lines.append(f"P=Copy prev coords: {prev_coords}")

    display = draw_info_overlay(display, lines)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.imshow(WINDOW_NAME, display)

    while True:
        # selectROI: kéo chuột → trả về (x, y, w, h) trong display coords
        roi = cv2.selectROI(WINDOW_NAME, display, fromCenter=False, printNotice=False)
        x_d, y_d, w_d, h_d = roi

        # Nếu w hoặc h = 0 → user chưa chọn hoặc nhấn C/ESC
        if w_d == 0 or h_d == 0:
            # Đọc key để phân biệt hành động
            key = cv2.waitKey(0) & 0xFF
            if key in (ord('q'), ord('Q')):
                cv2.destroyAllWindows()
                return "quit"
            if key in (ord('s'), ord('S')):
                return None
            if key in (ord('z'), ord('Z')):
                return "undo"
            if key in (ord('p'), ord('P')) and prev_coords:
                # Copy tọa độ từ listing trước
                done(f"  Copy tọa độ từ listing trước: {prev_coords}")
                _show_preview(img_bgr, prev_coords, display, scale)
                confirm = _confirm_roi(display)
                if confirm:
                    return prev_coords
                # Nếu không confirm → vòng lặp lại để chọn mới
                continue
            # Phím khác → chọn lại
            continue

        # Scale ngược về tọa độ ảnh gốc
        x = int(x_d / scale)
        y = int(y_d / scale)
        w = int(w_d / scale)
        h = int(h_d / scale)

        # Clamp trong bounds ảnh gốc
        x = max(0, min(x, W_orig - 1))
        y = max(0, min(y, H_orig - 1))
        w = min(w, W_orig - x)
        h = min(h, H_orig - y)

        coords = (x, y, w, h)
        done(f"  ROI gốc: x={x} y={y} w={w} h={h}")

        # Hiển thị preview crop trên display
        _show_preview(img_bgr, coords, display, scale)

        # Confirm
        key = cv2.waitKey(0) & 0xFF
        if key in (13, 32):       # ENTER hoặc SPACE → xác nhận
            cv2.destroyAllWindows()
            return coords
        if key in (ord('c'), ord('C')):   # C → chọn lại
            cv2.imshow(WINDOW_NAME, display)
            continue
        if key in (ord('s'), ord('S')):   # S → skip
            cv2.destroyAllWindows()
            return None
        if key in (ord('z'), ord('Z')):   # Z → undo
            cv2.destroyAllWindows()
            return "undo"
        if key in (ord('q'), ord('Q')):   # Q → quit
            cv2.destroyAllWindows()
            return "quit"
        # Phím khác → chọn lại
        cv2.imshow(WINDOW_NAME, display)


def _show_preview(img_bgr, coords, display, scale):
    """Vẽ rectangle và crop preview lên display."""
    x, y, w, h = coords
    # Scale về display coords
    x_d = int(x * scale); y_d = int(y * scale)
    w_d = int(w * scale); h_d = int(h * scale)

    preview = display.copy()
    cv2.rectangle(preview, (x_d, y_d), (x_d+w_d, y_d+h_d), (0, 255, 0), 2)

    # Crop nhỏ hiển thị góc dưới phải
    crop_orig = img_bgr[y:y+h, x:x+w]
    if crop_orig.size > 0:
        thumb_h = min(150, display.shape[0] // 3)
        thumb_w = int(crop_orig.shape[1] * thumb_h / crop_orig.shape[0])
        thumb = cv2.resize(crop_orig, (thumb_w, thumb_h))
        ph, pw = display.shape[:2]
        ty = ph - thumb_h - 10
        tx = pw - thumb_w - 10
        if ty > 0 and tx > 0:
            preview[ty:ty+thumb_h, tx:tx+thumb_w] = thumb
            cv2.rectangle(preview, (tx-1, ty-1), (tx+thumb_w+1, ty+thumb_h+1), (0,255,0), 1)

    cv2.putText(preview, "ENTER=OK  C=Chon lai", (10, display.shape[0]-10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    cv2.imshow(WINDOW_NAME, preview)


def _confirm_roi(display):
    key = cv2.waitKey(0) & 0xFF
    return key in (13, 32)   # ENTER hoặc SPACE


# ── Main ──────────────────────────────────────────────────────────────────────
def run_annotation(
    csv_path:  str,
    limit:     Optional[int],
    skip_done: bool = True,
):
    """
    Entry point annotation.
    skip_done=True: bỏ qua listing đã có tọa độ trong crop_coords.csv.
    """
    listings   = load_listings(csv_path, limit)
    coords_map = load_crop_log()
    prev_coords: Optional[Tuple[int,int,int,int]] = None

    # Lọc listing chưa có tọa độ
    if skip_done:
        pending = [r for r in listings if r["listing_id"] not in coords_map]
        skipped = len(listings) - len(pending)
        if skipped:
            info(f"Bỏ qua {skipped} listings đã có tọa độ.")
    else:
        pending = listings

    info(f"Cần annotation: {len(pending)} listings")
    if not pending:
        done("Tất cả đã có tọa độ!")
        return

    print(f"\n{'═'*60}")
    print(f"  ENTER/SPACE = xác nhận  |  C = chọn lại  |  S = skip")
    print(f"  Z = undo listing trước  |  P = copy coords trước  |  Q = quit")
    print(f"{'═'*60}\n")

    i = 0
    history: List[str] = []   # listing_id đã làm để undo

    while i < len(pending):
        row       = pending[i]
        lid       = row["listing_id"]
        shop      = row.get("shop_name", "")
        title     = row.get("title", "")[:55]
        image_url = row["image_1"]

        info(f"[{i+1}/{len(pending)}] {lid}  shop={shop}")

        # Download
        img_bgr = download_image(image_url)
        if img_bgr is None:
            warn(f"  Không download được ảnh — skip")
            i += 1
            continue

        # Annotate
        result = annotate_one(
            img_bgr, lid, shop, title,
            index=i+1, total=len(pending),
            prev_coords=prev_coords,
        )

        if result == "quit":
            info("Quit — lưu tất cả đã chọn.")
            break

        if result == "undo":
            if history:
                prev_lid = history.pop()
                coords_map.pop(prev_lid, None)
                # Tìm lại index của prev_lid
                prev_i = next((j for j, r in enumerate(pending) if r["listing_id"] == prev_lid), i-1)
                i = prev_i
                warn(f"  Undo → quay lại listing {prev_lid}")
                # Reset prev_coords
                prev_coords = list(coords_map.values())[-1] if coords_map else None
                save_crop_log(coords_map)
            else:
                warn("  Không có listing trước để undo.")
            continue

        if result is None:
            warn(f"  Skip {lid}")
            i += 1
            continue

        # Lưu tọa độ
        coords_map[lid] = result
        prev_coords     = result
        history.append(lid)

        # Lưu preview
        save_preview(img_bgr, result, lid)

        # Auto-save mỗi listing
        save_crop_log(coords_map)
        done(f"  Saved: x={result[0]} y={result[1]} w={result[2]} h={result[3]}")

        i += 1

    # Final save
    save_crop_log(coords_map)
    done(f"\nHoàn thành. Tổng {len(coords_map)} listings có tọa độ.")
    done(f"Chạy extract: python extract_art.py --batch --all")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Annotation tool — chọn vùng art bằng chuột")
    parser.add_argument("--csv",        default=INPUT_CSV, help="CSV input")
    parser.add_argument("--limit",      type=int,          help="Giới hạn số listing")
    parser.add_argument("--redo-all",   action="store_true", help="Redo cả listing đã có tọa độ")
    args = parser.parse_args()

    run_annotation(
        csv_path  = args.csv,
        limit     = args.limit,
        skip_done = not args.redo_all,
    )