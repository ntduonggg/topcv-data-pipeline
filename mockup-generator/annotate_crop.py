"""
annotate_crops.py — Tool annotation tọa độ crop art từ ảnh mockup
==================================================================
Flow:
  1. Đọc heyetsy_image_urls.csv → lấy image URLs + metadata
  2. Download tất cả ảnh hợp lệ (>= MIN_RES) cho listing
     → tự động bỏ qua ảnh thiếu URL hoặc không đủ res
  3. User duyệt ảnh bằng N/P, xác nhận bằng ENTER
  4. User kéo chuột chọn vùng art → ENTER xác nhận
  5. Lưu (x, y, w, h, image) vào crop_coords.csv
  6. Sang listing tiếp theo

Phím tắt — Bước chọn ảnh:
  ENTER / SPACE  → xác nhận ảnh hiện tại
  N              → ảnh tiếp theo
  P              → ảnh trước
  S              → skip listing
  Q              → quit

Phím tắt — Bước annotation ROI:
  ENTER / SPACE  → xác nhận vùng đã chọn
  C              → chọn lại (reset ROI)
  S              → skip listing
  Z              → quay lại listing trước (undo)
  P              → dùng tọa độ listing trước (copy)
  Q              → quit

Cài đặt:
  pip install opencv-python pillow requests pandas
"""

import os
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
    INFO  = "\033[94m"; WARN = "\033[93m"
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
INPUT_CSV   = "heyetsy_image_urls.csv"
CROP_LOG    = "crop_coords.csv"
PREVIEW_DIR = Path("extracted_art/previews")
MAX_DISPLAY = 900
IMAGE_COLS  = [f"image_{i}" for i in range(1, 6)]   # image_1 .. image_5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

WINDOW_SELECT   = "Chọn ảnh  |  N=Next  P=Prev  ENTER=Chon  S=Skip  Q=Quit"
WINDOW_ANNOTATE = "Annotate Art  |  ENTER=OK  C=Reset  S=Skip  Z=Undo  P=Copy prev  Q=Quit"
MIN_RES = 1028


# ── Download ──────────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[np.ndarray]:
    """Download ảnh từ URL → numpy array BGR."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        img_pil = Image.open(BytesIO(r.content)).convert("RGB")
        return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    except Exception as e:
        err(f"Download lỗi: {e}")
        return None


def download_valid_images(row: Dict) -> List[Tuple[np.ndarray, str, int]]:
    """
    Download tất cả ảnh đủ res (>= MIN_RES) cho listing.
    Trả về list (img_bgr, col_name, col_index) — ví dụ ("image_3", 3).
    Tự động bỏ qua URL trống và ảnh không đủ res.
    """
    valid: List[Tuple[np.ndarray, str, int]] = []
    for col in IMAGE_COLS:
        url = row.get(col, "")
        if not url:
            continue
        info(f"  Kiem tra {col} ...")
        img = download_image(url)
        if img is None:
            continue
        h, w = img.shape[:2]
        if w >= MIN_RES and h >= MIN_RES:
            idx = int(col.split("_")[1])
            info(f"  {col} OK ({w}x{h}px)")
            valid.append((img, col, idx))
        else:
            warn(f"  {col} không đủ res ({w}x{h}px < {MIN_RES}px) — bỏ qua")
    return valid


# ── Resize helper ─────────────────────────────────────────────────────────────
def resize_for_display(img: np.ndarray, max_size: int = MAX_DISPLAY) -> Tuple[np.ndarray, float]:
    H, W = img.shape[:2]
    scale = min(max_size / W, max_size / H, 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    return img, scale


# ── Crop log ──────────────────────────────────────────────────────────────────
def load_crop_log(path: str = CROP_LOG) -> Dict[str, Tuple[int, int, int, int, int]]:
    """Load tọa độ đã lưu. Tuple: (x, y, w, h, image_index)."""
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        coords = {}
        for _, row in df.iterrows():
            lid = row.get("listing_id", "")
            if lid:
                coords[lid] = (
                    int(row.get("x", 0)),
                    int(row.get("y", 0)),
                    int(row.get("w", 0)),
                    int(row.get("h", 0)),
                    int(row.get("image", 1)),   # default 1 cho data cũ không có cột image
                )
        return coords
    except Exception as e:
        warn(f"Không đọc được crop log: {e}")
        return {}


def save_crop_log(coords_map: Dict[str, Tuple[int, int, int, int, int]], path: str = CROP_LOG):
    """Atomic write: ghi .tmp → rename."""
    rows = [
        {"listing_id": lid, "x": c[0], "y": c[1], "w": c[2], "h": c[3], "image": c[4]}
        for lid, c in coords_map.items()
    ]
    tmp = path + ".tmp"
    pd.DataFrame(rows).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    done(f"Saved {len(rows)} coords -> {path}")


# ── Load listings ─────────────────────────────────────────────────────────────
def load_listings(csv_path: str, limit: Optional[int]) -> List[Dict]:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    if "listing_id" not in df.columns or "image_1" not in df.columns:
        raise ValueError("CSV phải có cột 'listing_id' và 'image_1'")
    for col in IMAGE_COLS:
        if col not in df.columns:
            df[col] = ""
    df = df[df["image_1"] != ""]
    if limit:
        df = df.head(limit)
    records = df.to_dict("records")
    info(f"Loaded {len(records)} listings từ {csv_path}")
    return records


# ── Save preview crop ─────────────────────────────────────────────────────────
def save_preview(img_bgr: np.ndarray, coords: Tuple[int, int, int, int, int], listing_id: str):
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    x, y, w, h = coords[:4]
    crop = img_bgr[y:y + h, x:x + w]
    cv2.imwrite(str(PREVIEW_DIR / f"{listing_id}_crop_preview.png"), crop)


# ── Draw overlay ──────────────────────────────────────────────────────────────

def draw_info_overlay(img: np.ndarray, text_lines: List[str]) -> np.ndarray:
    overlay = img.copy()
    y0 = 20
    for line in text_lines:
        cv2.putText(overlay, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(overlay, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        y0 += 22
    return overlay


# ── Image selection ───────────────────────────────────────────────────────────
def select_image_interactive(
    valid_images: List[Tuple[np.ndarray, str, int]],
    listing_id: str,
    shop_name: str,
    title: str,
    index: int,
    total: int,
) -> Tuple[Optional[np.ndarray], Optional[int]]:
    """
    Hiện ảnh carousel, user duyệt N/P và xác nhận bằng ENTER.
    Trả về (img_bgr, col_index) — ví dụ col_index=3 tương ứng image_3.
    Trả về (None, None) nếu skip listing.
    Trả về ("quit", None) nếu quit.
    """
    if not valid_images:
        return None, None

    # Chỉ 1 ảnh hợp lệ → tự động chọn, không cần navigation
    if len(valid_images) == 1:
        img, col, idx = valid_images[0]
        h, w = img.shape[:2]
        info(f"  Chỉ 1 ảnh hợp lệ ({col}, {w}x{h}px) — tự động chọn")
        return img, idx

    cur = 0
    cv2.namedWindow(WINDOW_SELECT, cv2.WINDOW_NORMAL)

    while True:
        img_bgr, col_name, col_idx = valid_images[cur]
        H_orig, W_orig = img_bgr.shape[:2]
        display, _ = resize_for_display(img_bgr)

        lines = [
            f"[{index}/{total}] {listing_id}  shop={shop_name}",
            f"Title: {title[:55]}",
            f"Anh: {col_name} ({W_orig}x{H_orig}px)  [{cur + 1}/{len(valid_images)} ảnh hợp lệ]",
            "N=Next  P=Prev  ENTER=Chọn ảnh này  S=Skip listing  Q=Quit",
        ]
        cv2.imshow(WINDOW_SELECT, draw_info_overlay(display, lines))
        key = cv2.waitKey(0) & 0xFF

        if key in (13, 32):                     # ENTER / SPACE → chọn ảnh này
            cv2.destroyWindow(WINDOW_SELECT)
            done(f"  Chọn {col_name} ({W_orig}x{H_orig}px)")
            return img_bgr, col_idx

        elif key in (ord('n'), ord('N')):
            cur = (cur + 1) % len(valid_images)

        elif key in (ord('p'), ord('P')):
            cur = (cur - 1) % len(valid_images)

        elif key in (ord('s'), ord('S')):
            cv2.destroyWindow(WINDOW_SELECT)
            return None, None

        elif key in (ord('q'), ord('Q')):
            cv2.destroyWindow(WINDOW_SELECT)
            return "quit", None
        # Phím khác → bỏ qua


# ── Core annotation ───────────────────────────────────────────────────────────
def annotate_one(
    img_bgr: np.ndarray,
    listing_id: str,
    shop_name: str,
    title: str,
    index: int,
    total: int,
    image_idx: int,
    prev_coords: Optional[Tuple[int, int, int, int]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Hiện ảnh đã chọn, user kéo chọn vùng art.
    Trả về (x, y, w, h) tọa độ thực (đã scale ngược).
    Trả về None nếu skip.
    Trả về 'undo' nếu user nhấn Z.
    Trả về 'quit' nếu user nhấn Q.
    """
    H_orig, W_orig = img_bgr.shape[:2]
    display, scale = resize_for_display(img_bgr)

    lines = [
        f"[{index}/{total}] {listing_id}  shop={shop_name}  image_{image_idx}",
        f"Title: {title[:55]}",
        f"Size: {W_orig}x{H_orig}px  (display scale: {scale:.2f})",
    ]
    if prev_coords:
        lines.append(f"P=Copy prev coords: {prev_coords}")

    display = draw_info_overlay(display, lines)
    cv2.namedWindow(WINDOW_ANNOTATE, cv2.WINDOW_NORMAL)
    cv2.imshow(WINDOW_ANNOTATE, display)

    while True:
        roi = cv2.selectROI(WINDOW_ANNOTATE, display, fromCenter=False, printNotice=False)
        x_d, y_d, w_d, h_d = roi

        if w_d == 0 or h_d == 0:
            # Chưa kéo ROI — đọc phím điều hướng
            key = cv2.waitKey(0) & 0xFF
            if key in (ord('q'), ord('Q')):
                cv2.destroyAllWindows()
                return "quit"
            if key in (ord('s'), ord('S')):
                cv2.destroyAllWindows()
                return None
            if key in (ord('z'), ord('Z')):
                cv2.destroyAllWindows()
                return "undo"
            if key in (ord('p'), ord('P')) and prev_coords:
                done(f"  Copy tọa độ từ listing trước: {prev_coords}")
                _show_preview(img_bgr, prev_coords, display, scale)
                if _confirm_roi():
                    cv2.destroyAllWindows()
                    return prev_coords
                cv2.imshow(WINDOW_ANNOTATE, display)
            continue

        # Scale ngược về tọa độ ảnh gốc + clamp
        x = max(0, min(int(x_d / scale), W_orig - 1))
        y = max(0, min(int(y_d / scale), H_orig - 1))
        w = min(int(w_d / scale), W_orig - x)
        h = min(int(h_d / scale), H_orig - y)
        coords = (x, y, w, h)
        done(f"  ROI gốc: x={x} y={y} w={w} h={h}")

        _show_preview(img_bgr, coords, display, scale)

        key = cv2.waitKey(0) & 0xFF
        if key in (13, 32):                     # ENTER / SPACE → xác nhận
            cv2.destroyAllWindows()
            return coords
        if key in (ord('c'), ord('C')):         # C → chọn lại
            cv2.imshow(WINDOW_ANNOTATE, display)
            continue
        if key in (ord('s'), ord('S')):
            cv2.destroyAllWindows()
            return None
        if key in (ord('z'), ord('Z')):
            cv2.destroyAllWindows()
            return "undo"
        if key in (ord('q'), ord('Q')):
            cv2.destroyAllWindows()
            return "quit"
        cv2.imshow(WINDOW_ANNOTATE, display)


def _show_preview(img_bgr: np.ndarray, coords: Tuple, display: np.ndarray, scale: float):
    """Vẽ rectangle + thumbnail crop góc dưới phải lên display."""
    x, y, w, h = coords[:4]
    x_d, y_d = int(x * scale), int(y * scale)
    w_d, h_d = int(w * scale), int(h * scale)

    preview = display.copy()
    cv2.rectangle(preview, (x_d, y_d), (x_d + w_d, y_d + h_d), (0, 255, 0), 2)

    crop_orig = img_bgr[y:y + h, x:x + w]
    if crop_orig.size > 0:
        thumb_h = min(150, display.shape[0] // 3)
        thumb_w = int(crop_orig.shape[1] * thumb_h / crop_orig.shape[0])
        thumb = cv2.resize(crop_orig, (thumb_w, thumb_h))
        ph, pw = display.shape[:2]
        ty, tx = ph - thumb_h - 10, pw - thumb_w - 10
        if ty > 0 and tx > 0:
            preview[ty:ty + thumb_h, tx:tx + thumb_w] = thumb
            cv2.rectangle(preview, (tx - 1, ty - 1), (tx + thumb_w + 1, ty + thumb_h + 1), (0, 255, 0), 1)

    cv2.putText(preview, "ENTER=OK  C=Chọn lại", (10, display.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    cv2.imshow(WINDOW_ANNOTATE, preview)


def _confirm_roi() -> bool:
    key = cv2.waitKey(0) & 0xFF
    return key in (13, 32)


# ── Main ──────────────────────────────────────────────────────────────────────
def run_annotation(csv_path: str, limit: Optional[int], skip_done: bool = True):
    listings   = load_listings(csv_path, limit)
    coords_map = load_crop_log()
    prev_coords: Optional[Tuple[int, int, int, int]] = None   # (x,y,w,h) của listing trước

    if skip_done:
        pending = [r for r in listings if r["listing_id"] not in coords_map]
        skipped = len(listings) - len(pending)
        if skipped:
            info(f"Bỏ qua {skipped} listings đã có tọa độ.")
    else:
        pending = listings

    info(f"Can annotation: {len(pending)} listings")
    if not pending:
        done("Tất cả đã có tọa độ!")
        return

    print(f"\n{'═'*60}")
    print(f"  Chọn ảnh : N=Next  P=Prev  ENTER=Chọn  S=Skip  Q=Quit")
    print(f"  ROI      : ENTER=OK  C=Reset  S=Skip  Z=Undo  P=Copy  Q=Quit")
    print(f"{'═'*60}\n")

    i = 0
    history: List[str] = []   # listing_id đã annotate (dùng cho undo)

    while i < len(pending):
        row   = pending[i]
        lid   = row["listing_id"]
        shop  = row.get("shop_name", "")
        title = row.get("title", "")[:55]

        info(f"[{i + 1}/{len(pending)}] {lid}  shop={shop}")

        # ── Phase 1: Download tất cả ảnh hợp lệ ──────────────────────────────
        valid_images = download_valid_images(row)
        if not valid_images:
            warn("  Không có ảnh nào hợp lệ — skip")
            i += 1
            continue

        # ── Phase 2: User chọn ảnh ───────────────────────────────────────────
        sel_img, image_idx = select_image_interactive(
            valid_images, lid, shop, title, index=i + 1, total=len(pending)
        )

        if isinstance(sel_img, str):            # "quit"
            info("Quit — lưu tất cả đã chọn.")
            break
        if sel_img is None:                     # skip
            warn(f"  Skip {lid}")
            i += 1
            continue
        img_bgr = sel_img

        # ── Phase 3: Annotation ROI ───────────────────────────────────────────
        result = annotate_one(
            img_bgr, lid, shop, title,
            index=i + 1, total=len(pending),
            image_idx=image_idx,
            prev_coords=prev_coords,
        )

        if result == "quit":
            info("Quit — lưu tất cả đã chọn.")
            break

        if result == "undo":
            if history:
                prev_lid = history.pop()
                coords_map.pop(prev_lid, None)
                prev_i = next((j for j, r in enumerate(pending) if r["listing_id"] == prev_lid), i - 1)
                i = prev_i
                warn(f"  Undo → quay lại listing {prev_lid}")
                prev_coords = coords_map[list(coords_map)[-1]][:4] if coords_map else None
                save_crop_log(coords_map)
            else:
                warn("  Không có listing trước để undo.")
            continue

        if result is None:
            warn(f"  Skip {lid}")
            i += 1
            continue

        # ── Lưu tọa độ + image index ──────────────────────────────────────────
        full_coords: Tuple[int, int, int, int, int] = (*result, image_idx)
        coords_map[lid] = full_coords
        prev_coords     = result              # (x,y,w,h) cho P=copy ở listing sau
        history.append(lid)

        save_preview(img_bgr, full_coords, lid)
        save_crop_log(coords_map)
        done(f"  Saved: x={result[0]} y={result[1]} w={result[2]} h={result[3]} image={image_idx}")

        i += 1

    save_crop_log(coords_map)
    done(f"\nHoàn thành. Tổng {len(coords_map)} listings có tọa độ.")
    done("Chạy extract: python extract_art.py --batch --all")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Annotation tool — chọn vùng art bằng chuột")
    parser.add_argument("--csv",      default=INPUT_CSV, help="CSV input")
    parser.add_argument("--limit",    type=int,          help="Giới hạn số listing")
    parser.add_argument("--redo-all", action="store_true", help="Redo tất cả listing đã có tọa độ")
    args = parser.parse_args()

    run_annotation(
        csv_path  = args.csv,
        limit     = args.limit,
        skip_done = not args.redo_all,
    )
