"""
annotate_crops.py — Tool annotation tọa độ crop art từ ảnh mockup
==================================================================
Flow:
  1. Đọc heyetsy_image_urls.csv → lấy image URLs + metadata
  2. Download tất cả ảnh hợp lệ (>= MIN_RES) cho listing
     → tự động bỏ qua ảnh thiếu URL hoặc không đủ res
  3. User duyệt ảnh bằng N/P, xác nhận bằng ENTER
  4. User kéo chuột chọn vùng art MẶT TRƯỚC → ENTER xác nhận
  5. (Tùy chọn) Nếu còn ảnh khác: B = chọn ảnh mặt lưng → annotate ROI lưng
  6. Lưu front + back coords vào crop_coords.csv
  7. Sang listing tiếp theo

Phím tắt — Bước chọn ảnh (mặt trước / mặt lưng):
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

Phím tắt — Xác nhận mặt lưng (sau khi annotation front xong):
  B              → có ảnh mặt lưng → chọn ảnh và annotate
  ENTER / SPACE  → không có mặt lưng → tiếp tục listing sau
  S              → skip listing
  Q              → quit

Cài đặt:
  pip install opencv-python pillow requests pandas
"""

import os
import sys
import argparse
from datetime import datetime

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
IMAGE_COLS  = [f"image_{i}" for i in range(1, 8)]   # image_1 .. image_7

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

WINDOW_SELECT      = "Chọn ảnh  |  N=Next  P=Prev  ENTER=Chon  S=Skip  Q=Quit"
WINDOW_ANNOTATE    = "Annotate Art  |  ENTER=OK  C=Reset  S=Skip  Z=Undo  P=Copy prev  Q=Quit"
WINDOW_BACK_PROMPT = "Mặt lưng?  B=Có (chọn ảnh)  ENTER=Không  S=Skip  Q=Quit"
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
def load_crop_log(path: str = CROP_LOG) -> Tuple[
    Dict[str, Tuple[int, int, int, int, int]],
    Dict[str, Tuple[int, int, int, int, int]],
    Dict[str, float],
    Dict[str, float],
]:
    """
    Load tọa độ đã lưu.
    Trả về (front_map, back_map, tilt_map, tilt_back_map).
    """
    if not os.path.exists(path):
        return {}, {}, {}, {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        front:      Dict[str, Tuple[int, int, int, int, int]] = {}
        back:       Dict[str, Tuple[int, int, int, int, int]] = {}
        tilt_map:      Dict[str, float] = {}
        tilt_back_map: Dict[str, float] = {}
        for _, row in df.iterrows():
            lid = row.get("listing_id", "")
            if not lid:
                continue
            front[lid] = (
                int(row.get("x", 0)),
                int(row.get("y", 0)),
                int(row.get("w", 0)),
                int(row.get("h", 0)),
                int(row.get("image", 1)),
            )
            if row.get("x_back", ""):
                back[lid] = (
                    int(row.get("x_back", 0)),
                    int(row.get("y_back", 0)),
                    int(row.get("w_back", 0)),
                    int(row.get("h_back", 0)),
                    int(row.get("image_back", 1)),
                )
            t = row.get("tilt", "")
            if t:
                try:
                    tilt_map[lid] = float(t)
                except ValueError:
                    pass
            tb = row.get("tilt_back", "")
            if tb:
                try:
                    tilt_back_map[lid] = float(tb)
                except ValueError:
                    pass
        return front, back, tilt_map, tilt_back_map
    except Exception as e:
        warn(f"Không đọc được crop log: {e}")
        return {}, {}, {}, {}


def save_crop_log(
    coords_map: Dict[str, Tuple[int, int, int, int, int]],
    back_coords_map: Optional[Dict[str, Tuple[int, int, int, int, int]]] = None,
    tilt_map: Optional[Dict[str, float]] = None,
    tilt_back_map: Optional[Dict[str, float]] = None,
    path: str = CROP_LOG,
):
    """Atomic write: ghi .tmp → rename. Hỗ trợ front, back coords và tilt."""
    all_lids = list(coords_map.keys())
    if back_coords_map:
        for lid in back_coords_map:
            if lid not in coords_map:
                all_lids.append(lid)

    rows = []
    for lid in all_lids:
        row: Dict = {"listing_id": lid}
        if lid in coords_map:
            c = coords_map[lid]
            row.update({"x": c[0], "y": c[1], "w": c[2], "h": c[3], "image": c[4]})
        if back_coords_map and lid in back_coords_map:
            bc = back_coords_map[lid]
            row.update({
                "x_back":     bc[0],
                "y_back":     bc[1],
                "w_back":     bc[2],
                "h_back":     bc[3],
                "image_back": bc[4],
            })
        row["tilt"]      = tilt_map.get(lid, 0) if tilt_map else 0
        row["tilt_back"] = tilt_back_map.get(lid, 0) if tilt_back_map else 0
        rows.append(row)

    tmp = path + ".tmp"
    pd.DataFrame(rows).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    back_cnt = len(back_coords_map) if back_coords_map else 0
    done(f"Saved {len(rows)} listings ({back_cnt} có mặt lưng) -> {path}")


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
def save_preview(img_bgr: np.ndarray, coords: Tuple, listing_id: str, suffix: str = ""):
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    x, y, w, h = coords[:4]
    crop = img_bgr[y:y + h, x:x + w]
    filename = f"{listing_id}_crop_preview{suffix}.png"
    cv2.imwrite(str(PREVIEW_DIR / filename), crop)


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
    exclude_indices: Optional[List[int]] = None,
    side_label: str = "MAT TRUOC",
) -> Tuple[Optional[np.ndarray], Optional[int]]:
    """
    Hiện ảnh carousel, user duyệt N/P và xác nhận bằng ENTER.
    exclude_indices: bỏ qua các col_index đã được chọn ở bước khác.
    side_label: nhãn hiển thị ("MAT TRUOC" hoặc "MAT LUNG").
    Trả về (img_bgr, col_index), (None, None) nếu skip, ("quit", None) nếu quit.
    """
    pool = [im for im in valid_images if im[2] not in (exclude_indices or [])]
    if not pool:
        return None, None

    # Chỉ 1 ảnh trong pool → tự động chọn, không cần navigation
    if len(pool) == 1:
        img, col, idx = pool[0]
        h, w = img.shape[:2]
        info(f"  [{side_label}] Chỉ 1 ảnh ({col}, {w}x{h}px) — tự động chọn")
        return img, idx

    cur = 0
    win = f"[{side_label}] " + WINDOW_SELECT
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    while True:
        img_bgr, col_name, col_idx = pool[cur]
        H_orig, W_orig = img_bgr.shape[:2]
        display, _ = resize_for_display(img_bgr)

        lines = [
            f"[{side_label}] [{index}/{total}] {listing_id}  shop={shop_name}",
            f"Title: {title[:55]}",
            f"Anh: {col_name} ({W_orig}x{H_orig}px)  [{cur + 1}/{len(pool)} anh hop le]",
            "N=Next  P=Prev  ENTER=Chon anh nay  S=Skip listing  Q=Quit",
        ]
        cv2.imshow(win, draw_info_overlay(display, lines))
        key = cv2.waitKey(0) & 0xFF

        if key in (13, 32):                     # ENTER / SPACE
            cv2.destroyWindow(win)
            done(f"  [{side_label}] Chon {col_name} ({W_orig}x{H_orig}px)")
            return img_bgr, col_idx

        elif key in (ord('n'), ord('N')):
            cur = (cur + 1) % len(pool)

        elif key in (ord('p'), ord('P')):
            cur = (cur - 1) % len(pool)

        elif key in (ord('s'), ord('S')):
            cv2.destroyWindow(win)
            return None, None

        elif key in (ord('q'), ord('Q')):
            cv2.destroyWindow(win)
            return "quit", None


# ── Prompt for back image ─────────────────────────────────────────────────────
def prompt_for_back(
    front_img: np.ndarray,
    front_coords: Tuple[int, int, int, int],
    listing_id: str,
    remaining_count: int,
) -> str:
    """
    Hiện ảnh mặt trước với ROI đã annotate, hỏi có ảnh mặt lưng không.
    Trả về: "back" | "no_back" | "skip" | "quit".
    """
    display, scale = resize_for_display(front_img)
    x, y, w, h = front_coords
    x_d, y_d = int(x * scale), int(y * scale)
    w_d, h_d = int(w * scale), int(h * scale)

    preview = display.copy()
    cv2.rectangle(preview, (x_d, y_d), (x_d + w_d, y_d + h_d), (0, 255, 0), 2)
    cv2.putText(preview, "MAT TRUOC", (x_d + 4, y_d + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    lines = [
        f"Mat truoc da annotation: {listing_id}",
        f"Con {remaining_count} anh khac trong listing nay.",
        "Co anh MAT LUNG khong?",
        "B = Co (chon anh mat lung)   ENTER = Khong   S = Skip   Q = Quit",
    ]
    preview = draw_info_overlay(preview, lines)

    cv2.namedWindow(WINDOW_BACK_PROMPT, cv2.WINDOW_NORMAL)
    cv2.imshow(WINDOW_BACK_PROMPT, preview)

    while True:
        key = cv2.waitKey(0) & 0xFF
        if key in (ord('b'), ord('B')):
            cv2.destroyWindow(WINDOW_BACK_PROMPT)
            return "back"
        elif key in (13, 32):
            cv2.destroyWindow(WINDOW_BACK_PROMPT)
            return "no_back"
        elif key in (ord('s'), ord('S')):
            cv2.destroyWindow(WINDOW_BACK_PROMPT)
            return "skip"
        elif key in (ord('q'), ord('Q')):
            cv2.destroyWindow(WINDOW_BACK_PROMPT)
            return "quit"


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
    side_label: str = "MAT TRUOC",
) -> Optional[Tuple[int, int, int, int]]:
    """
    Hiện ảnh đã chọn, user kéo chọn vùng art.
    Trả về (x, y, w, h), None nếu skip, 'undo' hoặc 'quit'.
    """
    H_orig, W_orig = img_bgr.shape[:2]
    display, scale = resize_for_display(img_bgr)

    win = f"[{side_label}] " + WINDOW_ANNOTATE
    lines = [
        f"[{side_label}] [{index}/{total}] {listing_id}  shop={shop_name}  image_{image_idx}",
        f"Title: {title[:55]}",
        f"Size: {W_orig}x{H_orig}px  (display scale: {scale:.2f})",
    ]
    if prev_coords:
        lines.append(f"P=Copy prev coords: {prev_coords}")

    display = draw_info_overlay(display, lines)
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.imshow(win, display)

    while True:
        roi = cv2.selectROI(win, display, fromCenter=False, printNotice=False)
        x_d, y_d, w_d, h_d = roi

        if w_d == 0 or h_d == 0:
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
                _show_preview(img_bgr, prev_coords, display, scale, win)
                if _confirm_roi():
                    cv2.destroyAllWindows()
                    return prev_coords
                cv2.imshow(win, display)
            continue

        x = max(0, min(int(x_d / scale), W_orig - 1))
        y = max(0, min(int(y_d / scale), H_orig - 1))
        w = min(int(w_d / scale), W_orig - x)
        h = min(int(h_d / scale), H_orig - y)
        coords = (x, y, w, h)
        done(f"  [{side_label}] ROI gốc: x={x} y={y} w={w} h={h}")

        _show_preview(img_bgr, coords, display, scale, win)

        key = cv2.waitKey(0) & 0xFF
        if key in (13, 32):
            cv2.destroyAllWindows()
            return coords
        if key in (ord('c'), ord('C')):
            cv2.imshow(win, display)
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
        cv2.imshow(win, display)


def estimate_tilt(crop_bgr: np.ndarray) -> Optional[float]:
    """
    Ước lượng góc nghiêng của art trong vùng crop bằng HoughLinesP.

    Flow:
      1. Xám hóa → Canny edge detection
      2. HoughLinesP → tập các đoạn thẳng nổi bật
      3. Tính góc từng đoạn → lấy trung vị (bỏ qua đường ngang/dọc thuần túy)
      4. Trả về góc lệch so với trục ngang [-45°, 45°]

    Trả về None nếu không đủ đường thẳng để ước lượng.
    """
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)

    lines = cv2.HoughLinesP(
        edges,
        rho=1, theta=np.pi / 180,
        threshold=60,
        minLineLength=max(30, min(crop_bgr.shape[:2]) // 8),
        maxLineGap=10,
    )
    if lines is None or len(lines) < 5:
        return None

    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 == x1:
            continue
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        # Bỏ đường gần thẳng đứng (>70°) — thường là viền ảnh
        if abs(angle) > 70:
            continue
        # Chuẩn hóa về [-45, 45]
        angle = ((angle + 45) % 90) - 45
        angles.append(angle)

    if len(angles) < 5:
        return None

    return round(float(np.median(angles)), 1)


def _show_preview(
    img_bgr: np.ndarray,
    coords: Tuple,
    display: np.ndarray,
    scale: float,
    win: str = WINDOW_ANNOTATE,
):
    """Vẽ rectangle + thumbnail crop góc dưới phải + tilt angle lên display."""
    x, y, w, h = coords[:4]
    x_d, y_d = int(x * scale), int(y * scale)
    w_d, h_d = int(w * scale), int(h * scale)

    preview = display.copy()
    cv2.rectangle(preview, (x_d, y_d), (x_d + w_d, y_d + h_d), (0, 255, 0), 2)

    crop_orig = img_bgr[y:y + h, x:x + w]
    if crop_orig.size > 0:
        # Tilt detection
        tilt = estimate_tilt(crop_orig)
        if tilt is not None:
            color = (0, 255, 0) if abs(tilt) <= 3 else (0, 165, 255) if abs(tilt) <= 8 else (0, 0, 255)
            label = f"Nghieng: {tilt:+.1f} deg"
            cv2.putText(preview, label, (x_d + 4, y_d + h_d - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3)
            cv2.putText(preview, label, (x_d + 4, y_d + h_d - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)

        # Thumbnail crop góc dưới phải
        thumb_h = min(150, display.shape[0] // 3)
        thumb_w = int(crop_orig.shape[1] * thumb_h / crop_orig.shape[0])
        thumb = cv2.resize(crop_orig, (thumb_w, thumb_h))
        ph, pw = display.shape[:2]
        ty, tx = ph - thumb_h - 10, pw - thumb_w - 10
        if ty > 0 and tx > 0:
            preview[ty:ty + thumb_h, tx:tx + thumb_w] = thumb
            cv2.rectangle(preview, (tx - 1, ty - 1), (tx + thumb_w + 1, ty + thumb_h + 1), (0, 255, 0), 1)

    cv2.putText(preview, "ENTER=OK  C=Chon lai", (10, display.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    cv2.imshow(win, preview)


def _confirm_roi() -> bool:
    key = cv2.waitKey(0) & 0xFF
    return key in (13, 32)


# ── Main ──────────────────────────────────────────────────────────────────────
def run_annotation(
    csv_path: str,
    limit: Optional[int],
    skip_done: bool = True,
    listing_ids: Optional[List[str]] = None,
):
    listings   = load_listings(csv_path, limit=None if listing_ids else limit)
    coords_map, back_coords_map, tilt_map, tilt_back_map = load_crop_log()
    prev_front_coords: Optional[Tuple[int, int, int, int]] = None
    prev_back_coords:  Optional[Tuple[int, int, int, int]] = None

    if listing_ids:
        id_set   = set(listing_ids)
        listings = [r for r in listings if r["listing_id"] in id_set]
        not_found = id_set - {r["listing_id"] for r in listings}
        if not_found:
            warn(f"Không tìm thấy listing IDs: {', '.join(sorted(not_found))}")
        info(f"Lọc theo listing_ids: {len(listings)} listings")
        skip_done = False

    if limit and not listing_ids:
        listings = listings[:limit]

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
    print(f"  Mat lung : B=Co mat lung  ENTER=Khong  S=Skip  Q=Quit")
    print(f"{'═'*60}\n")

    i = 0
    history: List[str] = []

    while i < len(pending):
        row   = pending[i]
        lid   = row["listing_id"]
        shop  = row.get("shop_name", "")
        title = row.get("title", "")[:55]

        info(f"[{i + 1}/{len(pending)}] {lid}  shop={shop}")

        # ── Phase 1: Download ─────────────────────────────────────────────────
        valid_images = download_valid_images(row)
        if not valid_images:
            warn("  Không có ảnh nào hợp lệ — skip")
            i += 1
            continue

        # ── Phase 2: Chọn ảnh mặt trước ──────────────────────────────────────
        sel_img, image_idx = select_image_interactive(
            valid_images, lid, shop, title,
            index=i + 1, total=len(pending),
            side_label="MAT TRUOC",
        )

        if isinstance(sel_img, str):
            info("Quit — lưu tất cả đã chọn.")
            break
        if sel_img is None:
            warn(f"  Skip {lid}")
            i += 1
            continue

        # ── Phase 3: Annotate mặt trước ───────────────────────────────────────
        result = annotate_one(
            sel_img, lid, shop, title,
            index=i + 1, total=len(pending),
            image_idx=image_idx,
            prev_coords=prev_front_coords,
            side_label="MAT TRUOC",
        )

        if result == "quit":
            info("Quit — lưu tất cả đã chọn.")
            break

        if result == "undo":
            if history:
                prev_lid = history.pop()
                coords_map.pop(prev_lid, None)
                back_coords_map.pop(prev_lid, None)
                prev_i = next((j for j, r in enumerate(pending) if r["listing_id"] == prev_lid), i - 1)
                i = prev_i
                warn(f"  Undo → quay lại listing {prev_lid}")
                prev_front_coords = coords_map[list(coords_map)[-1]][:4] if coords_map else None
                prev_back_coords  = back_coords_map[list(back_coords_map)[-1]][:4] if back_coords_map else None
                save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
            else:
                warn("  Không có listing trước để undo.")
            continue

        if result is None:
            warn(f"  Skip {lid}")
            i += 1
            continue

        # ── Lưu mặt trước ────────────────────────────────────────────────────
        front_coords: Tuple[int, int, int, int, int] = (*result, image_idx)
        coords_map[lid]  = front_coords
        prev_front_coords = result
        history.append(lid)

        # Tính tilt từ vùng crop đã chọn
        x_f, y_f, w_f, h_f = result
        front_crop = sel_img[y_f:y_f + h_f, x_f:x_f + w_f]
        front_tilt = estimate_tilt(front_crop)
        tilt_map[lid] = front_tilt if front_tilt is not None else 0.0
        done(f"  [MAT TRUOC] x={result[0]} y={result[1]} w={result[2]} h={result[3]} image={image_idx}  tilt={tilt_map[lid]:+.1f}°")

        save_preview(sel_img, front_coords, lid, suffix="_front")

        # ── Phase 4: Hỏi mặt lưng ────────────────────────────────────────────
        remaining = [im for im in valid_images if im[2] != image_idx]
        if remaining:
            back_choice = prompt_for_back(sel_img, result, lid, len(remaining))

            if back_choice == "quit":
                save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
                info("Quit — lưu tất cả đã chọn.")
                break

            if back_choice == "skip":
                save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
                warn(f"  Skip {lid}")
                i += 1
                continue

            if back_choice == "back":
                # ── Phase 5: Chọn ảnh mặt lưng ───────────────────────────────
                back_sel, back_img_idx = select_image_interactive(
                    valid_images, lid, shop, title,
                    index=i + 1, total=len(pending),
                    exclude_indices=[image_idx],
                    side_label="MAT LUNG",
                )

                if isinstance(back_sel, str):
                    save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
                    info("Quit — lưu tất cả đã chọn.")
                    break

                if back_sel is not None:
                    # ── Phase 6: Annotate mặt lưng ────────────────────────────
                    back_result = annotate_one(
                        back_sel, lid, shop, title,
                        index=i + 1, total=len(pending),
                        image_idx=back_img_idx,
                        prev_coords=prev_back_coords,
                        side_label="MAT LUNG",
                    )

                    if isinstance(back_result, str) and back_result == "quit":
                        save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
                        info("Quit — lưu tất cả đã chọn.")
                        break

                    if back_result and back_result != "undo":
                        back_full: Tuple[int, int, int, int, int] = (*back_result, back_img_idx)
                        back_coords_map[lid] = back_full
                        prev_back_coords     = back_result

                        x_b, y_b, w_b, h_b = back_result
                        back_crop = back_sel[y_b:y_b + h_b, x_b:x_b + w_b]
                        back_tilt = estimate_tilt(back_crop)
                        tilt_back_map[lid] = back_tilt if back_tilt is not None else 0.0
                        done(f"  [MAT LUNG] x={back_result[0]} y={back_result[1]} w={back_result[2]} h={back_result[3]} image={back_img_idx}  tilt={tilt_back_map[lid]:+.1f}°")

                        save_preview(back_sel, back_full, lid, suffix="_back")
                    elif back_result == "undo":
                        # Undo toàn bộ listing này (front lẫn back)
                        coords_map.pop(lid, None)
                        back_coords_map.pop(lid, None)
                        if history and history[-1] == lid:
                            history.pop()
                        if history:
                            prev_lid_undo = history[-1]
                            prev_front_coords = coords_map.get(prev_lid_undo, (0,0,0,0,0))[:4]
                            prev_back_coords  = back_coords_map.get(prev_lid_undo, (0,0,0,0,0))[:4] if prev_lid_undo in back_coords_map else None
                        save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
                        warn(f"  Undo listing {lid} — quay lại.")
                        continue

        save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
        i += 1

    save_crop_log(coords_map, back_coords_map, tilt_map, tilt_back_map)
    back_cnt = len(back_coords_map)
    done(f"\nHoàn thành. Tổng {len(coords_map)} listings có tọa độ, {back_cnt} có mặt lưng.")
    done("Chạy extract: python extract_art.py --batch --all")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Annotation tool — chọn vùng art bằng chuột (hỗ trợ mặt trước + mặt lưng)")
    parser.add_argument("--csv",         default=INPUT_CSV, help="CSV input")
    parser.add_argument("--limit",       type=int,          help="Giới hạn số listing")
    parser.add_argument("--redo-all",    action="store_true", help="Redo tất cả listing đã có tọa độ")
    parser.add_argument("--listing-ids", nargs="+", metavar="ID",
                        help="Chỉ annotate các listing ID cụ thể (space-separated), tự động redo")
    args = parser.parse_args()

    run_annotation(
        csv_path    = args.csv,
        limit       = args.limit,
        skip_done   = not args.redo_all,
        listing_ids = args.listing_ids,
    )
