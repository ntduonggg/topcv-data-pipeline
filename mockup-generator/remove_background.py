"""
remove_background.py — 2-pass rembg pipeline (hỗ trợ mặt trước + mặt lưng)
=============================================================================
Input:
  - crop_coords.csv         : listing_id, x, y, w, h, image
                              (optional) x_back, y_back, w_back, h_back, image_back
  - heyetsy_image_urls.csv  : listing_id, image_1..image_N, shop_name, title

Pipeline mỗi listing:
  1. Download + crop → lưu preview gốc (không enhance)
  2. Enhance (contrast, saturation, sharpness) → enhanced crop
  3. rembg pass 1 → art thô (rough mask)
  4. Apply alpha mask lên preview gốc → masked_preview
     (background transparent, foreground giữ màu gốc)
  5. rembg pass 2 → art tinh (refine edges trên input đã sạch)
  6. Post-process alpha (noise, despeckle, close holes)
  7. Lưu kết quả → extracted_art/arts/{lid}_art.png
  8. Nếu có back coords → lặp lại bước 1-7 cho ảnh mặt lưng
     → extracted_art/arts/{lid}_art_back.png

Dùng:
  python mockup-generator/remove_background.py --limit 5
  python mockup-generator/remove_background.py --preset matting
  python mockup-generator/remove_background.py --redo
  python mockup-generator/remove_background.py --back-only   # chỉ xử lý back còn thiếu
  python mockup-generator/remove_background.py --no-back     # bỏ qua back, chỉ front
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

import numpy as np
import requests
import pandas as pd
from PIL import Image, ImageEnhance, ImageFilter

# Fix Unicode output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from rembg import remove, new_session
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

try:
    from scipy.ndimage import label as _ndlabel, binary_closing
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


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
OUTPUT_DIR       = Path("extracted_art/compare_model")
PREVIEW_DIR      = Path("extracted_art/previews")
EXTRACT_LOG      = "extract_log.csv"
CHECKPOINT_EVERY = 50

REMBG_MODEL = "isnet-general-use"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Cột back trong CSV (optional)
BACK_COORD_COLS = {"x_back", "y_back", "w_back", "h_back", "image_back"}


# ── Setup ─────────────────────────────────────────────────────────────────────
def setup_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


# ── Load inputs ───────────────────────────────────────────────────────────────
def load_crop_coords(path: str = CROP_COORDS_CSV) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path} — chạy annotate_crops.py trước.")
    df = pd.read_csv(path, dtype=str).fillna("")
    required = {"listing_id", "x", "y", "w", "h"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"crop_coords.csv thiếu cột: {missing}")

    for col in ["x", "y", "w", "h"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["image"] = pd.to_numeric(df.get("image", 1), errors="coerce").fillna(1).astype(int)

    # Parse cột back (optional — có thể thiếu hoặc rỗng)
    for col in ["x_back", "y_back", "w_back", "h_back"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        else:
            df[col] = 0
    if "image_back" in df.columns:
        df["image_back"] = pd.to_numeric(df["image_back"], errors="coerce").fillna(0).astype(int)
    else:
        df["image_back"] = 0

    # Cờ has_back: w_back > 0 và h_back > 0
    df["has_back"] = (df["w_back"] > 0) & (df["h_back"] > 0)

    invalid = df[(df["w"] == 0) | (df["h"] == 0)]
    if len(invalid):
        warn(f"Bỏ {len(invalid)} dòng có front coords không hợp lệ.")
        df = df[(df["w"] > 0) & (df["h"] > 0)]

    back_count = df["has_back"].sum()
    info(f"Loaded {len(df)} crop coords từ {path}  ({back_count} có back coords)")
    return df


def load_image_urls(path: str = IMAGE_URLS_CSV) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path}")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "listing_id" not in df.columns or "image_1" not in df.columns:
        raise ValueError(f"{path} cần có cột 'listing_id' và 'image_1'")
    info(f"Loaded {len(df)} rows từ {path}")
    return df


def _pick_url_by_image_index(row: Dict, image_idx: int, side: str = "front") -> Tuple[str, str]:
    """Lấy URL theo image index. Fallback về image_1 nếu cột trống."""
    col = f"image_{int(image_idx)}" if image_idx > 0 else "image_1"
    url = row.get(col, "")
    if url:
        return url, col
    fallback_url = row.get("image_1", "")
    if fallback_url:
        warn(f"  {row['listing_id']}: [{side}] {col} trống — fallback image_1")
    return fallback_url, "image_1"


def build_work_list(
    coords_df: pd.DataFrame,
    urls_df:   pd.DataFrame,
    skip_done:  bool = True,
    back_only:  bool = False,
    process_back: bool = True,
) -> List[Dict]:
    image_cols = [c for c in urls_df.columns if c.startswith("image_")]
    keep_cols  = ["listing_id"] + image_cols + \
                 [c for c in ["shop_name", "title"] if c in urls_df.columns]
    merged = coords_df.merge(urls_df[keep_cols], on="listing_id", how="left")

    work = []
    for _, row in merged.iterrows():
        item = row.to_dict()
        lid  = item["listing_id"]

        # Front URL
        front_url, front_col = _pick_url_by_image_index(item, int(item.get("image", 1)), "front")
        item["image_url"] = front_url
        item["image_col"] = front_col

        # Back URL (chỉ khi has_back)
        if item.get("has_back") and process_back:
            back_url, back_col = _pick_url_by_image_index(item, int(item.get("image_back", 1)), "back")
            item["back_image_url"] = back_url
            item["back_image_col"] = back_col
        else:
            item["back_image_url"] = ""
            item["back_image_col"] = ""

        if not front_url:
            warn(f"  {lid}: không có front URL — bỏ qua.")
            continue

        work.append(item)

    if skip_done and not back_only:
        before = len(work)
        work   = [r for r in work if not (OUTPUT_DIR / f"{r['listing_id']}_art.png").exists()]
        skipped = before - len(work)
        if skipped:
            info(f"Bỏ qua {skipped} front đã có output PNG.")

    if back_only:
        # Chỉ giữ listing có back coords VÀ chưa có back PNG
        work = [
            r for r in work
            if r.get("has_back")
            and not (OUTPUT_DIR / f"{r['listing_id']}_art_back.png").exists()
        ]
        info(f"--back-only: {len(work)} listings cần extract back art.")
    elif process_back and skip_done:
        # Đánh dấu back nào đã xong để bỏ qua trong loop
        for r in work:
            if r.get("has_back") and (OUTPUT_DIR / f"{r['listing_id']}_art_back.png").exists():
                r["back_skip_done"] = True
            else:
                r["back_skip_done"] = False

    info(f"Cần xử lý: {len(work)} listings")
    return work


# ── Download ──────────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[Image.Image]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        err(f"Download lỗi: {e}")
        return None


# ── Crop ──────────────────────────────────────────────────────────────────────
def crop_image(img: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    W, H = img.size
    return img.crop((max(0, x), max(0, y), min(W, x + w), min(H, y + h)))


# ── Pre-processing ────────────────────────────────────────────────────────────
def _apply_to_rgb(img: Image.Image, fn) -> Image.Image:
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        result = fn(img.convert("RGB")).convert("RGBA")
        result.putalpha(alpha)
        return result
    return fn(img)


def enhance_contrast(img: Image.Image, factor: float = 1.4) -> Image.Image:
    return _apply_to_rgb(img, lambda i: ImageEnhance.Contrast(i).enhance(factor))


def enhance_sharpness(img: Image.Image, radius: int = 2, percent: int = 150, threshold: int = 3) -> Image.Image:
    return img.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold))


def enhance_saturation(img: Image.Image, factor: float = 1.2) -> Image.Image:
    return _apply_to_rgb(img, lambda i: ImageEnhance.Color(i).enhance(factor))


def preprocess(
    img: Image.Image,
    contrast: float = 1.2,
    saturation: float = 1.15,
    sharpness_percent: int = 130,
) -> Image.Image:
    out = enhance_contrast(img, contrast)
    out = enhance_saturation(out, saturation)
    out = enhance_sharpness(out, radius=2, percent=sharpness_percent, threshold=3)
    return out


# ── rembg ─────────────────────────────────────────────────────────────────────
def _run_rembg(
    img: Image.Image,
    session,
    alpha_matting: bool = False,
    fg_threshold: int = 240,
    bg_threshold: int = 10,
    erode_size: int = 10,
) -> Image.Image:
    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    raw = buf.getvalue()

    if alpha_matting:
        try:
            result_bytes = remove(
                raw, session=session,
                alpha_matting=True,
                alpha_matting_foreground_threshold=fg_threshold,
                alpha_matting_background_threshold=bg_threshold,
                alpha_matting_erode_size=erode_size,
            )
        except Exception as e:
            if "Cholesky" in str(e) or "positive-definite" in str(e) or "positive_definite" in str(e):
                warn("  Alpha matting thất bại (Cholesky) — fallback standard rembg.")
                result_bytes = remove(raw, session=session)
            else:
                raise
    else:
        result_bytes = remove(raw, session=session)

    return Image.open(BytesIO(result_bytes)).convert("RGBA")


# ── Mask utils ────────────────────────────────────────────────────────────────
def apply_mask_to_image(img: Image.Image, mask_art: Image.Image) -> Image.Image:
    """Apply alpha channel từ mask_art lên img gốc (RGB hoặc RGBA)."""
    mw, mh = mask_art.size
    if img.size != (mw, mh):
        img = img.resize((mw, mh), Image.LANCZOS)
    alpha = mask_art.getchannel("A")
    rgb   = np.array(img.convert("RGB"), dtype=np.uint8)
    a     = np.array(alpha, dtype=np.uint8)
    return Image.fromarray(np.dstack([rgb, a]), mode="RGBA")


# ── Post-processing alpha ─────────────────────────────────────────────────────
def clean_alpha_noise(img_rgba: Image.Image, min_alpha: int = 15) -> Image.Image:
    arr = np.array(img_rgba)
    arr[arr[:, :, 3] < min_alpha, 3] = 0
    return Image.fromarray(arr)


def smooth_alpha_edges(img_rgba: Image.Image, radius: float = 1.2) -> Image.Image:
    arr = np.array(img_rgba, dtype=np.float32)
    alpha_pil = Image.fromarray(arr[:, :, 3].astype(np.uint8), mode="L")
    alpha_blurred = alpha_pil.filter(ImageFilter.GaussianBlur(radius=radius))
    arr[:, :, 3] = np.array(alpha_blurred, dtype=np.float32)
    return Image.fromarray(arr.astype(np.uint8))


def despeckle_alpha(img_rgba: Image.Image, min_area: int = 200) -> Image.Image:
    if not SCIPY_AVAILABLE:
        warn("despeckle_alpha: scipy không có — bỏ qua.")
        return img_rgba
    arr = np.array(img_rgba)
    mask = arr[:, :, 3] > 0
    labeled, n_components = _ndlabel(mask)
    for comp_id in range(1, n_components + 1):
        if (labeled == comp_id).sum() < min_area:
            arr[labeled == comp_id, 3] = 0
    return Image.fromarray(arr)


def close_alpha_holes(img_rgba: Image.Image, iterations: int = 2) -> Image.Image:
    if not SCIPY_AVAILABLE:
        warn("close_alpha_holes: scipy không có — bỏ qua.")
        return img_rgba
    arr = np.array(img_rgba)
    mask = arr[:, :, 3] > 127
    closed = binary_closing(mask, iterations=iterations)
    arr[closed & ~mask, 3] = 255
    return Image.fromarray(arr)


def postprocess_colors(
    img_rgba: Image.Image,
    contrast: float = 1.2,
    saturation: float = 1.2,
) -> Image.Image:
    """Tăng contrast/saturation cho output cuối — chỉ áp lên RGB, giữ nguyên alpha."""
    if contrast == 1.0 and saturation == 1.0:
        return img_rgba
    alpha = img_rgba.getchannel("A")
    rgb = img_rgba.convert("RGB")
    if contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
    if saturation != 1.0:
        rgb = ImageEnhance.Color(rgb).enhance(saturation)
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def postprocess_alpha(
    img_rgba: Image.Image,
    threshold: int = 15,
    smooth: bool = True,
    smooth_radius: float = 1.0,
    despeckle: bool = False,
    min_area: int = 200,
    close_holes: bool = False,
    close_iterations: int = 2,
) -> Image.Image:
    out = clean_alpha_noise(img_rgba, min_alpha=threshold)
    if despeckle:
        out = despeckle_alpha(out, min_area=min_area)
    if close_holes:
        out = close_alpha_holes(out, iterations=close_iterations)
    if smooth:
        out = smooth_alpha_edges(out, radius=smooth_radius)
    return out


# ── Text / detail protection ─────────────────────────────────────────────────
def protect_text_regions(
    original_crop: Image.Image,
    result: Image.Image,
    canny_lo: int = 30,
    canny_hi: int = 110,
    text_dilate_px: int = 5,
    near_art_px: int = 30,
) -> Image.Image:
    """
    Bảo vệ vùng text/chi tiết không bị rembg xóa nhầm.

    Logic:
      1. Canny edge detection trên crop gốc → text_mask (vùng biên sắc = text/line art)
      2. Dilate text_mask để bao phủ thân chữ (không chỉ viền)
      3. Tính near_art = vùng trong vòng near_art_px pixel của art đã keep
         (tránh restore những pixel hoàn toàn nằm ngoài art)
      4. Với mọi pixel thuộc (text_mask ∩ near_art):
           - alpha > 0  → snap lên 255  (hết bán-transparent)
           - alpha == 0 → restore lên 255 (lấy lại pixel bị xóa nhầm)
    """
    if original_crop.size != result.size:
        original_crop = original_crop.resize(result.size, Image.LANCZOS)

    orig_rgb = np.array(original_crop.convert("RGB"), dtype=np.uint8)
    res_arr  = np.array(result.convert("RGBA"),       dtype=np.uint8)
    alpha    = res_arr[:, :, 3]

    # ── Edge detection → text_mask ────────────────────────────────────────────
    if CV2_AVAILABLE:
        gray  = cv2.cvtColor(orig_rgb, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, canny_lo, canny_hi)
        k     = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (text_dilate_px * 2 + 1, text_dilate_px * 2 + 1))
        text_mask = cv2.dilate(edges, k, iterations=2).astype(bool)
    else:
        # PIL fallback khi không có cv2
        gray_arr  = np.array(Image.fromarray(orig_rgb).convert("L"))
        gx = np.abs(np.diff(gray_arr.astype(np.int16), axis=1, prepend=gray_arr[:, :1]))
        gy = np.abs(np.diff(gray_arr.astype(np.int16), axis=0, prepend=gray_arr[:1, :]))
        edges_arr = np.clip(gx + gy, 0, 255).astype(np.uint8)
        text_mask = edges_arr > canny_lo
        # Dilation thủ công bằng max pooling đơn giản
        from scipy.ndimage import binary_dilation
        text_mask = binary_dilation(text_mask, iterations=text_dilate_px)

    # ── near_art: trong vòng near_art_px của art đang giữ ────────────────────
    kept = (alpha > 50).astype(np.uint8) * 255
    if CV2_AVAILABLE:
        k2       = np.ones((near_art_px * 2 + 1, near_art_px * 2 + 1), np.uint8)
        near_art = cv2.dilate(kept, k2, iterations=1).astype(bool)
    elif SCIPY_AVAILABLE:
        from scipy.ndimage import binary_dilation as _bd
        near_art = _bd(alpha > 50, iterations=near_art_px // 2)
    else:
        near_art = np.ones_like(alpha, dtype=bool)   # không có cả 2 → apply toàn bộ

    # ── Áp dụng: chỉ snap semi-transparent, không restore pixel đã bị xóa ────
    protect = text_mask & near_art
    snap_mask = protect & (alpha > 0)
    res_arr[snap_mask, 3] = 255

    n_snapped = int((snap_mask & (alpha < 255)).sum())
    if n_snapped:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [text-protect] snap={n_snapped}px")

    return Image.fromarray(res_arr)


# ── Pipeline presets ──────────────────────────────────────────────────────────
_TEXT_PROTECT_DEFAULTS = dict(
    protect_text=True,
    text_canny_lo=60, text_canny_hi=150,
    text_dilate_px=2, text_near_art_px=15,
)

PRESETS: Dict[str, Dict] = {
    "standard": dict(
        contrast=1, saturation=1, sharpness_percent=100,
        alpha_matting_p1=False, fg_threshold_p1=240, bg_threshold_p1=10, erode_size_p1=10,
        alpha_matting_p2=False,
        post_threshold=15, post_smooth=False,
        post_despeckle=False, post_close_holes=True,
        post_contrast=1.15, post_saturation=1.2,
        **_TEXT_PROTECT_DEFAULTS,
    ),
    "enhanced": dict(
        contrast=1.7, saturation=1.5, sharpness_percent=200,
        alpha_matting_p1=False,
        alpha_matting_p2=False,
        post_threshold=15, post_smooth=True, smooth_radius=1.0,
        post_despeckle=False, post_close_holes=False,
        post_contrast=1.15, post_saturation=1.2,
        **_TEXT_PROTECT_DEFAULTS,
    ),
    "matting": dict(
        contrast=1.5, saturation=1.2, sharpness_percent=180,
        alpha_matting_p1=True, fg_threshold_p1=240, bg_threshold_p1=10, erode_size_p1=15,
        alpha_matting_p2=False,
        post_threshold=20, post_smooth=True, smooth_radius=1.5,
        post_despeckle=True, min_area=150,
        post_close_holes=True, close_iterations=2,
        post_contrast=1.15, post_saturation=1.2,
        **_TEXT_PROTECT_DEFAULTS,
    ),
    "aggressive": dict(
        contrast=1.6, saturation=1.3, sharpness_percent=200,
        alpha_matting_p1=True, fg_threshold_p1=250, bg_threshold_p1=15, erode_size_p1=15,
        alpha_matting_p2=True, fg_threshold_p2=240, bg_threshold_p2=10, erode_size_p2=10,
        post_threshold=25, post_smooth=True, smooth_radius=2.0,
        post_despeckle=True, min_area=300,
        post_close_holes=True, close_iterations=3,
        post_contrast=1.15, post_saturation=1.2,
        **_TEXT_PROTECT_DEFAULTS,
    ),
}


def run_two_pass_pipeline(
    cropped: Image.Image,
    session,
    preset: str = "standard",
    protect_text: Optional[bool] = None,
) -> Image.Image:
    """
    2-pass rembg pipeline:
      Pass 1: enhanced crop → rembg → rough mask
      Apply:  rough mask × original preview → masked_preview
      Pass 2: masked_preview → rembg → refined art
      Post:   alpha cleanup → (optional) text/detail protection
    protect_text=None → dùng giá trị trong preset (mặc định True).
    """
    cfg = PRESETS.get(preset)
    if cfg is None:
        raise ValueError(f"Preset '{preset}' không hợp lệ. Chọn: {list(PRESETS)}")

    enhanced = preprocess(
        cropped,
        contrast=cfg.get("contrast", 1.0),
        saturation=cfg.get("saturation", 1.0),
        sharpness_percent=cfg.get("sharpness_percent", 100),
    )

    art1 = _run_rembg(
        enhanced, session,
        alpha_matting=cfg.get("alpha_matting_p1", False),
        fg_threshold=cfg.get("fg_threshold_p1", 240),
        bg_threshold=cfg.get("bg_threshold_p1", 10),
        erode_size=cfg.get("erode_size_p1", 10),
    )

    masked_preview = apply_mask_to_image(cropped, art1)

    art2 = _run_rembg(
        masked_preview, session,
        alpha_matting=cfg.get("alpha_matting_p2", False),
        fg_threshold=cfg.get("fg_threshold_p2", 240),
        bg_threshold=cfg.get("bg_threshold_p2", 10),
        erode_size=cfg.get("erode_size_p2", 10),
    )

    result = postprocess_alpha(
        art2,
        threshold=cfg.get("post_threshold", 15),
        smooth=cfg.get("post_smooth", False),
        smooth_radius=cfg.get("smooth_radius", 1.0),
        despeckle=cfg.get("post_despeckle", False),
        min_area=cfg.get("min_area", 200),
        close_holes=cfg.get("post_close_holes", False),
        close_iterations=cfg.get("close_iterations", 2),
    )

    # ── Text / detail protection ──────────────────────────────────────────────
    do_protect = protect_text if protect_text is not None else cfg.get("protect_text", True)
    if do_protect:
        result = protect_text_regions(
            cropped, result,
            canny_lo=cfg.get("text_canny_lo", 30),
            canny_hi=cfg.get("text_canny_hi", 110),
            text_dilate_px=cfg.get("text_dilate_px", 5),
            near_art_px=cfg.get("text_near_art_px", 30),
        )

    # ── Post color enhancement (áp lên output cuối, giữ alpha) ───────────────
    result = postprocess_colors(
        result,
        contrast=cfg.get("post_contrast", 1.0),
        saturation=cfg.get("post_saturation", 1.0),
    )

    return result


# ── Log ───────────────────────────────────────────────────────────────────────
def flush_log(log: List[Dict], path: str = EXTRACT_LOG):
    if not log:
        return
    tmp = path + ".tmp"
    pd.DataFrame(log).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Log flush {len(log)} records → {path}")


def setup_signal(log: List[Dict]):
    def _handler(sig, frame):
        print()
        warn("Ctrl+C — flush log rồi thoát...")
        flush_log(log)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── Single-side extraction helper ────────────────────────────────────────────
def _extract_one_side(
    image_url: str,
    x: int, y: int, w: int, h: int,
    session,
    preset: str,
    out_path: Path,
    preview_path: Optional[Path],
    lid: str,
    side: str,
    protect_text: Optional[bool] = None,
    force_redo: bool = False,
) -> Tuple[str, float]:
    """
    Download → crop → 2-pass pipeline → save.
    Trả về (status, elapsed_s).
    status: "done" | "download_failed" | "invalid_coords" | "failed" | "skipped_done"
    """
    if out_path.exists() and not force_redo:
        info(f"  [{side}] Đã có {out_path.name} — skip")
        return "skipped_done", 0.0

    info(f"  [{side}] Download {image_url[:60]}...")
    img = download_image(image_url)
    if img is None:
        return "download_failed", 0.0

    W_img, H_img = img.size
    if x >= W_img or y >= H_img:
        err(f"  [{side}] Tọa độ vượt bounds ảnh ({W_img}x{H_img}) — skip")
        return "invalid_coords", 0.0

    cropped = crop_image(img, x, y, w, h)

    if preview_path:
        cropped.convert("RGB").save(preview_path)

    info(f"  [{side}] 2-pass rembg [{preset}] crop={w}x{h}px...")
    t0 = time.time()
    try:
        result  = run_two_pass_pipeline(cropped, session, preset=preset, protect_text=protect_text)
        elapsed = round(time.time() - t0, 1)
    except Exception as e:
        err(f"  [{side}] Pipeline lỗi: {e}")
        return "failed", 0.0

    result.save(out_path, format="PNG")
    done(f"  [{side}] Saved ({elapsed}s) → {out_path.name}")
    return "done", elapsed


# ── Main pipeline ─────────────────────────────────────────────────────────────
def extract_art(
    crop_csv:     str              = CROP_COORDS_CSV,
    image_csv:    str              = IMAGE_URLS_CSV,
    skip_done:    bool             = True,
    save_preview: bool             = True,
    limit:        int | None       = None,
    preset:       str              = "standard",
    process_back: bool             = True,
    back_only:    bool             = False,
    listing_ids:  List[str] | None = None,
    protect_text: bool             = True,
):
    if not REMBG_AVAILABLE:
        err("rembg chưa cài. Chạy: pip install \"rembg[cpu]\"")
        sys.exit(1)

    setup_dirs()

    coords_df = load_crop_coords(crop_csv)
    urls_df   = load_image_urls(image_csv)

    # Lọc theo listing_ids trước khi build work list
    if listing_ids:
        id_set    = set(listing_ids)
        before    = len(coords_df)
        coords_df = coords_df[coords_df["listing_id"].isin(id_set)]
        not_found = id_set - set(coords_df["listing_id"])
        if not_found:
            warn(f"Không tìm thấy trong crop CSV: {', '.join(sorted(not_found))}")
        info(f"--listing-ids: lọc {before} → {len(coords_df)} listings  (redo bỏ qua skip_done)")
        skip_done = False   # chỉ định ID cụ thể → luôn redo

    work      = build_work_list(
        coords_df, urls_df,
        skip_done=skip_done,
        back_only=back_only,
        process_back=process_back,
    )

    if limit:
        work = work[:limit]
        info(f"--limit {limit}: xử lý {len(work)} listings đầu tiên.")

    if not work:
        done("Không có gì để xử lý.")
        return pd.DataFrame()

    cfg = PRESETS[preset]
    info(f"Preset: {preset}  |  model: {REMBG_MODEL}")
    info(f"  pre  : contrast={cfg.get('contrast')}  sat={cfg.get('saturation')}  sharp={cfg.get('sharpness_percent')}")
    info(f"  pass1: matting={cfg.get('alpha_matting_p1', False)}")
    info(f"  pass2: matting={cfg.get('alpha_matting_p2', False)}")
    info(f"  post : threshold={cfg.get('post_threshold')}  smooth={cfg.get('post_smooth')}  "
         f"despeckle={cfg.get('post_despeckle')}  close_holes={cfg.get('post_close_holes')}")
    info(f"  back : process_back={process_back}  back_only={back_only}")

    info(f"Khởi tạo rembg session (model: {REMBG_MODEL})...")
    session = new_session(REMBG_MODEL)
    done("rembg sẵn sàng.")

    log: List[Dict] = []
    setup_signal(log)

    total = len(work)
    items_since_flush = 0

    for i, row in enumerate(work, 1):
        lid   = row["listing_id"]
        shop  = row.get("shop_name", "")
        title = str(row.get("title", ""))[:50]

        x, y, w, h = int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])
        image_url  = row["image_url"]
        image_col  = row.get("image_col", "image_1")

        has_back      = bool(row.get("has_back", False)) and process_back
        back_skip_done = bool(row.get("back_skip_done", False))

        front_out  = OUTPUT_DIR / f"{lid}_art.png"
        back_out   = OUTPUT_DIR / f"{lid}_art_back.png"
        front_prev = PREVIEW_DIR / f"{lid}_crop_preview_front.png" if save_preview else None
        back_prev  = PREVIEW_DIR / f"{lid}_crop_preview_back.png"  if save_preview else None

        print(f"\n{'─'*55}")
        info(f"[{i}/{total}] {lid}  shop={shop}")
        info(f"  Title: {title}")
        info(f"  Front: x={x} y={y} w={w} h={h}  src={image_col}")
        if has_back:
            xb = int(row.get("x_back", 0))
            yb = int(row.get("y_back", 0))
            wb = int(row.get("w_back", 0))
            hb = int(row.get("h_back", 0))
            back_col = row.get("back_image_col", "?")
            info(f"  Back : x={xb} y={yb} w={wb} h={hb}  src={back_col}")

        # ── Extract mặt trước ─────────────────────────────────────────────────
        front_status = "skipped_back_only"
        front_elapsed = 0.0

        if not back_only:
            front_status, front_elapsed = _extract_one_side(
                image_url, x, y, w, h,
                session, preset,
                front_out, front_prev,
                lid, "FRONT",
                protect_text=protect_text,
                force_redo=not skip_done,
            )

        # ── Extract mặt lưng ──────────────────────────────────────────────────
        back_status  = "no_back"
        back_elapsed = 0.0

        if has_back:
            if back_skip_done and not back_only:
                info(f"  [BACK] Đã có {back_out.name} — skip")
                back_status = "skipped_done"
            else:
                xb = int(row.get("x_back", 0))
                yb = int(row.get("y_back", 0))
                wb = int(row.get("w_back", 0))
                hb = int(row.get("h_back", 0))
                back_url = row.get("back_image_url", "")

                if not back_url:
                    warn(f"  [BACK] Không có URL — bỏ qua.")
                    back_status = "no_url"
                else:
                    # Nếu cùng ảnh nguồn với front thì dùng lại URL (vẫn download lại — đơn giản hơn cache)
                    back_status, back_elapsed = _extract_one_side(
                        back_url, xb, yb, wb, hb,
                        session, preset,
                        back_out, back_prev,
                        lid, "BACK",
                        protect_text=protect_text,
                        force_redo=not skip_done,
                    )

        log.append({
            "listing_id":    lid,
            "shop_name":     shop,
            "title":         title,
            "image_col":     image_col,
            "preset":        preset,
            "front_status":  front_status,
            "back_status":   back_status,
            "front_output":  str(front_out) if front_status == "done" else "",
            "back_output":   str(back_out)  if back_status  == "done" else "",
            "front_elapsed": front_elapsed,
            "back_elapsed":  back_elapsed,
        })
        items_since_flush += 1

        if items_since_flush >= CHECKPOINT_EVERY:
            flush_log(log)
            items_since_flush = 0

    flush_log(log)

    df_log = pd.DataFrame(log)

    front_done = (df_log["front_status"] == "done").sum()       if not df_log.empty else 0
    back_done  = (df_log["back_status"]  == "done").sum()       if not df_log.empty else 0
    front_fail = df_log["front_status"].isin(["download_failed","invalid_coords","failed"]).sum() if not df_log.empty else 0
    back_fail  = df_log["back_status"].isin(["download_failed","invalid_coords","failed"]).sum()  if not df_log.empty else 0
    back_total = (df_log["back_status"] != "no_back").sum()     if not df_log.empty else 0

    avg_front = df_log[df_log["front_status"] == "done"]["front_elapsed"].astype(float).mean() if front_done else 0
    avg_back  = df_log[df_log["back_status"]  == "done"]["back_elapsed"].astype(float).mean()  if back_done  else 0

    print(f"\n{'═'*55}")
    done(f"Preset: {preset}  |  Tổng listings: {total}")
    done(f"Front  ✓ {front_done}  ✗ {front_fail}  (~{avg_front:.1f}s/ảnh)")
    if back_total > 0:
        done(f"Back   ✓ {back_done}/{back_total}  ✗ {back_fail}  (~{avg_back:.1f}s/ảnh)")
    done(f"Output: {OUTPUT_DIR}/")
    done(f"Log: {EXTRACT_LOG}")

    return df_log


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="2-pass rembg pipeline — tách art từ mockup áo (front + back)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Presets:",
            "  standard   — enhance + 2-pass rembg, không matting (baseline)",
            "  enhanced   — enhance mạnh hơn + smooth alpha",
            "  matting    — pass 1 có alpha matting + despeckle + fill holes",
            "  aggressive — tất cả thông số mạnh nhất, cả 2 pass đều matting",
            "",
            "Pipeline mỗi listing:",
            "  download → crop → enhance → rembg(pass1) → mask × preview → rembg(pass2) → post → save",
            "  (lặp lại cho back nếu có back coords)",
            "",
            "Ví dụ:",
            "  python mockup-generator/remove_background.py --limit 5",
            "  python mockup-generator/remove_background.py --preset matting --limit 10",
            "  python mockup-generator/remove_background.py --preset aggressive --redo",
            "  python mockup-generator/remove_background.py --back-only   # chỉ extract back còn thiếu",
            "  python mockup-generator/remove_background.py --no-back     # bỏ qua back art",
            "  python mockup-generator/remove_background.py --listing-ids 123 456 789  # redo listing cụ thể",
        ]),
    )
    parser.add_argument("--crop-csv",     default=CROP_COORDS_CSV)
    parser.add_argument("--image-csv",    default=IMAGE_URLS_CSV)
    parser.add_argument("--redo",         action="store_true",    help="Redo listing đã có output PNG")
    parser.add_argument("--no-preview",   action="store_true",    help="Không lưu preview crop")
    parser.add_argument("--limit",        type=int, default=None, help="Giới hạn số listing")
    parser.add_argument("--preset",       default="standard",     choices=list(PRESETS),
                        help="Pipeline preset (default: standard)")
    parser.add_argument("--no-back",          action="store_true", help="Bỏ qua back art, chỉ xử lý front")
    parser.add_argument("--back-only",        action="store_true", help="Chỉ extract back art còn thiếu (bỏ qua front)")
    parser.add_argument("--listing-ids",      nargs="+", metavar="ID",
                        help="Chỉ xử lý các listing ID cụ thể (space-separated), tự động redo")
    parser.add_argument("--no-protect-text",  action="store_true",
                        help="Tắt text/detail protection (mặc định: bật)")
    args = parser.parse_args()

    if args.back_only and args.no_back:
        parser.error("--back-only và --no-back không thể dùng cùng nhau.")

    df = extract_art(
        crop_csv     = args.crop_csv,
        image_csv    = args.image_csv,
        skip_done    = not args.redo,
        save_preview = not args.no_preview,
        limit        = args.limit,
        preset       = args.preset,
        process_back = not args.no_back,
        back_only    = args.back_only,
        listing_ids  = args.listing_ids,
        protect_text = not args.no_protect_text,
    )

    if not df.empty:
        display_cols = ["listing_id", "preset", "front_status", "back_status",
                        "front_elapsed", "back_elapsed"]
        print(df[[c for c in display_cols if c in df.columns]].to_string(index=False))
