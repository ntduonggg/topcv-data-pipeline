"""
remove_background.py — 2-pass rembg pipeline
=============================================
Input:
  - crop_coords.csv         : listing_id, x, y, w, h, image
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

Dùng:
  python mockup-generator/remove_background.py --limit 5
  python mockup-generator/remove_background.py --preset matting
  python mockup-generator/remove_background.py --redo
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
OUTPUT_DIR       = Path("extracted_art/enhanced_art")
PREVIEW_DIR      = Path("extracted_art/previews")
EXTRACT_LOG      = "extract_log.csv"
CHECKPOINT_EVERY = 50

REMBG_MODEL = "isnet-general-use"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


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
    if "image" in df.columns:
        df["image"] = pd.to_numeric(df["image"], errors="coerce").fillna(1).astype(int)
    else:
        df["image"] = 1
    invalid = df[(df["w"] == 0) | (df["h"] == 0)]
    if len(invalid):
        warn(f"Bỏ {len(invalid)} dòng có tọa độ không hợp lệ.")
        df = df[(df["w"] > 0) & (df["h"] > 0)]
    info(f"Loaded {len(df)} crop coords từ {path}")
    return df


def load_image_urls(path: str = IMAGE_URLS_CSV) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path}")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "listing_id" not in df.columns or "image_1" not in df.columns:
        raise ValueError(f"{path} cần có cột 'listing_id' và 'image_1'")
    info(f"Loaded {len(df)} rows từ {path}")
    return df


def _pick_image_url(row: Dict) -> Tuple[str, str]:
    col = f"image_{int(row.get('image', 1))}"
    url = row.get(col, "")
    if url:
        return url, col
    fallback_url = row.get("image_1", "")
    if fallback_url:
        warn(f"  {row['listing_id']}: {col} trống — fallback image_1")
    return fallback_url, "image_1"


def build_work_list(
    coords_df: pd.DataFrame,
    urls_df:   pd.DataFrame,
    skip_done: bool = True,
) -> List[Dict]:
    image_cols = [c for c in urls_df.columns if c.startswith("image_")]
    keep_cols  = ["listing_id"] + image_cols + \
                 [c for c in ["shop_name", "title"] if c in urls_df.columns]
    merged = coords_df.merge(urls_df[keep_cols], on="listing_id", how="left")
    results = merged.apply(_pick_image_url, axis=1, result_type="expand")
    merged["image_url"] = results[0]
    merged["image_col"] = results[1]
    no_url = merged[merged["image_url"] == ""]
    if len(no_url):
        warn(f"{len(no_url)} listings không có image URL — bỏ qua.")
        merged = merged[merged["image_url"] != ""]
    work = merged.to_dict("records")
    if skip_done:
        before = len(work)
        work   = [r for r in work if not (OUTPUT_DIR / f"{r['listing_id']}_art.png").exists()]
        skipped = before - len(work)
        if skipped:
            info(f"Bỏ qua {skipped} listings đã có output PNG.")
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
    contrast: float = 1.4,
    saturation: float = 1.15,
    sharpness_percent: int = 150,
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
    """
    Apply alpha channel từ mask_art lên img gốc (RGB hoặc RGBA).
    img được resize về kích thước mask_art nếu cần.
    Trả về RGBA với RGB từ img gốc, alpha từ mask_art.
    """
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


# ── Pipeline presets ──────────────────────────────────────────────────────────
PRESETS: Dict[str, Dict] = {
    "standard": dict(
        # Pass 1
        contrast=1.2, saturation=1.2, sharpness_percent=130,
        alpha_matting_p1=False,
        # Pass 2
        alpha_matting_p2=False,
        # Post
        post_threshold=15, post_smooth=False,
        post_despeckle=False, post_close_holes=False,
    ),
    "enhanced": dict(
        contrast=1.7, saturation=1.5, sharpness_percent=200,
        alpha_matting_p1=False,
        alpha_matting_p2=False,
        post_threshold=15, post_smooth=True, smooth_radius=1.0,
        post_despeckle=False, post_close_holes=False,
    ),
    "matting": dict(
        contrast=1.5, saturation=1.2, sharpness_percent=180,
        alpha_matting_p1=True, fg_threshold_p1=240, bg_threshold_p1=10, erode_size_p1=10,
        alpha_matting_p2=False,
        post_threshold=20, post_smooth=True, smooth_radius=1.5,
        post_despeckle=True, min_area=150,
        post_close_holes=True, close_iterations=2,
    ),
    "aggressive": dict(
        contrast=1.6, saturation=1.3, sharpness_percent=200,
        alpha_matting_p1=True, fg_threshold_p1=250, bg_threshold_p1=15, erode_size_p1=15,
        alpha_matting_p2=True, fg_threshold_p2=240, bg_threshold_p2=10, erode_size_p2=10,
        post_threshold=25, post_smooth=True, smooth_radius=2.0,
        post_despeckle=True, min_area=300,
        post_close_holes=True, close_iterations=3,
    ),
}


def run_two_pass_pipeline(
    cropped: Image.Image,
    session,
    preset: str = "standard",
) -> Image.Image:
    """
    2-pass rembg pipeline:
      Pass 1: enhanced crop → rembg → rough mask
      Apply:  rough mask × original preview → masked_preview
      Pass 2: masked_preview → rembg → refined art
      Post:   alpha cleanup
    """
    cfg = PRESETS.get(preset)
    if cfg is None:
        raise ValueError(f"Preset '{preset}' không hợp lệ. Chọn: {list(PRESETS)}")

    # ── Pass 1: enhanced → rough mask ────────────────────────────────────────
    enhanced = preprocess(
        cropped,
        contrast=cfg.get("contrast", 1.4),
        saturation=cfg.get("saturation", 1.15),
        sharpness_percent=cfg.get("sharpness_percent", 150),
    )

    art1 = _run_rembg(
        enhanced, session,
        alpha_matting=cfg.get("alpha_matting_p1", False),
        fg_threshold=cfg.get("fg_threshold_p1", 240),
        bg_threshold=cfg.get("bg_threshold_p1", 10),
        erode_size=cfg.get("erode_size_p1", 10),
    )

    # ── Apply mask lên ảnh gốc (không enhance) ───────────────────────────────
    # → pass 2 nhận input đã sạch background, giữ màu gốc không bị ảnh hưởng bởi enhance
    masked_preview = apply_mask_to_image(cropped, art1)

    # ── Pass 2: masked preview → refined art ─────────────────────────────────
    art2 = _run_rembg(
        masked_preview, session,
        alpha_matting=cfg.get("alpha_matting_p2", False),
        fg_threshold=cfg.get("fg_threshold_p2", 240),
        bg_threshold=cfg.get("bg_threshold_p2", 10),
        erode_size=cfg.get("erode_size_p2", 10),
    )

    # ── Post-process ─────────────────────────────────────────────────────────
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


# ── Main pipeline ─────────────────────────────────────────────────────────────
def extract_art(
    crop_csv:     str        = CROP_COORDS_CSV,
    image_csv:    str        = IMAGE_URLS_CSV,
    skip_done:    bool       = True,
    save_preview: bool       = True,
    limit:        int | None = None,
    preset:       str        = "standard",
):
    if not REMBG_AVAILABLE:
        err("rembg chưa cài. Chạy: pip install \"rembg[cpu]\"")
        sys.exit(1)

    setup_dirs()

    coords_df = load_crop_coords(crop_csv)
    urls_df   = load_image_urls(image_csv)
    work      = build_work_list(coords_df, urls_df, skip_done=skip_done)

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

    info(f"Khởi tạo rembg session (model: {REMBG_MODEL})...")
    session = new_session(REMBG_MODEL)
    done("rembg sẵn sàng.")

    log: List[Dict] = []
    setup_signal(log)

    total = len(work)
    items_since_flush = 0

    for i, row in enumerate(work, 1):
        lid        = row["listing_id"]
        x, y, w, h = int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])
        image_url  = row["image_url"]
        image_col  = row.get("image_col", "image_1")
        shop       = row.get("shop_name", "")
        title      = str(row.get("title", ""))[:50]
        out_path   = OUTPUT_DIR / f"{lid}_art.png"

        print(f"\n{'─'*55}")
        info(f"[{i}/{total}] {lid}  shop={shop}")
        info(f"  Title: {title}")
        info(f"  Coords: x={x} y={y} w={w} h={h}  src={image_col}")

        img = download_image(image_url)
        if img is None:
            log.append({"listing_id": lid, "preset": preset, "status": "download_failed",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        W_img, H_img = img.size
        if x >= W_img or y >= H_img:
            err(f"  Tọa độ vượt bounds ảnh ({W_img}x{H_img}) — skip")
            log.append({"listing_id": lid, "preset": preset, "status": "invalid_coords",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        cropped = crop_image(img, x, y, w, h)

        if save_preview:
            prev_path = PREVIEW_DIR / f"{lid}_crop_preview.png"
            cropped.convert("RGB").save(prev_path)

        info(f"  2-pass rembg [{preset}]...")
        t0 = time.time()
        try:
            result  = run_two_pass_pipeline(cropped, session, preset=preset)
            elapsed = round(time.time() - t0, 1)
        except Exception as e:
            err(f"  Pipeline lỗi: {e}")
            log.append({"listing_id": lid, "preset": preset, "status": "failed",
                        "output": "", "elapsed_s": ""})
            items_since_flush += 1
            continue

        result.save(out_path, format="PNG")
        done(f"  Saved ({elapsed}s) → {out_path.name}")

        log.append({
            "listing_id": lid,
            "shop_name":  shop,
            "title":      title,
            "image_col":  image_col,
            "preset":     preset,
            "status":     "done",
            "output":     str(out_path),
            "elapsed_s":  elapsed,
        })
        items_since_flush += 1

        if items_since_flush >= CHECKPOINT_EVERY:
            flush_log(log)
            items_since_flush = 0

    flush_log(log)

    df_log  = pd.DataFrame(log)
    success = (df_log["status"] == "done").sum() if not df_log.empty else 0
    failed  = len(log) - success
    avg_t   = df_log[df_log["status"] == "done"]["elapsed_s"].astype(float).mean() if success else 0

    print(f"\n{'═'*55}")
    done(f"Preset: {preset}  |  Tổng: {total}  ✓ {success}  ✗ {failed}")
    done(f"Thời gian TB/ảnh: {avg_t:.1f}s  (2 lần rembg + pre/post)")
    done(f"Output: {OUTPUT_DIR}/")
    done(f"Log: {EXTRACT_LOG}")

    return df_log


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="2-pass rembg pipeline — tách art từ mockup áo",
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
            "",
            "Ví dụ:",
            "  python mockup-generator/remove_background.py --limit 5",
            "  python mockup-generator/remove_background.py --preset matting --limit 10",
            "  python mockup-generator/remove_background.py --preset aggressive --redo",
        ]),
    )
    parser.add_argument("--crop-csv",   default=CROP_COORDS_CSV)
    parser.add_argument("--image-csv",  default=IMAGE_URLS_CSV)
    parser.add_argument("--redo",       action="store_true",    help="Redo listing đã có output PNG")
    parser.add_argument("--no-preview", action="store_true",    help="Không lưu preview crop")
    parser.add_argument("--limit",      type=int, default=None, help="Giới hạn số listing")
    parser.add_argument("--preset",     default="standard",     choices=list(PRESETS),
                        help="Pipeline preset (default: standard)")
    args = parser.parse_args()

    df = extract_art(
        crop_csv     = args.crop_csv,
        image_csv    = args.image_csv,
        skip_done    = not args.redo,
        save_preview = not args.no_preview,
        limit        = args.limit,
        preset       = args.preset,
    )

    if not df.empty:
        print(df[["listing_id", "preset", "status", "elapsed_s"]].to_string(index=False))
