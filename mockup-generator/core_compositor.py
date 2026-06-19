"""
core_compositor.py — Engine ghép art lên mockup người mẫu
==========================================================
Logic lõi dùng chung cho Single, 2-Sides và Compose batch.

Cung cấp:
  - ANSI color logging (C, ts, info, warn, ckpt, done, err)
  - Perspective warp (OpenCV)
  - Blend modes: multiply / overlay / screen / normal
  - Shadow overlay (texture vải xuyên qua art)
  - Scale + anchor placement
  - Rotation (degree, chiều kim đồng hồ)
  - Auto blend mode theo tone màu áo trong tên file
  - Shape detection từ bounding box alpha

Lưu ý: Art đầu vào phải đã xóa phông trước (PNG có alpha channel).
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

# Fix Unicode output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


# ── Utility ───────────────────────────────────────────────────────────────────
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def get_art_files(folder: Path) -> list:
    return sorted([
        f for f in folder.rglob("*")
        if f.suffix.lower() in SUPPORTED_EXTS and f.is_file()
    ])


def save_image(img: Image.Image, path: Path, fmt: str = "PNG"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt.upper() in ("JPG", "JPEG"):
        img = img.convert("RGB")
        img.save(path, "JPEG", quality=95)
    else:
        img.save(path, "PNG")


# ── Xử lý art đầu vào ────────────────────────────────────────────────────────
def process_art(img: Image.Image) -> Image.Image:
    """Chuyển art sang RGBA. Art phải đã xóa phông trước."""
    return img.convert("RGBA")


# ── Perspective warp ──────────────────────────────────────────────────────────
def perspective_warp(art: Image.Image, src_pts: list, dst_pts: list,
                     output_size: tuple) -> Image.Image:
    """
    Warp art từ src_pts → dst_pts bằng OpenCV.
    src_pts: 4 góc art gốc [[x,y], ...]
    dst_pts: 4 điểm đích trên mockup [[x,y], ...]
    output_size: (width, height) canvas đầu ra
    """
    try:
        import cv2
        src = np.float32(src_pts)
        dst = np.float32(dst_pts)
        M = cv2.getPerspectiveTransform(src, dst)
        art_np = np.array(art.convert("RGBA"))
        warped = cv2.warpPerspective(
            art_np, M, output_size,
            flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        )
        return Image.fromarray(warped)
    except ImportError:
        warn("[WARP] opencv-python chưa cài — bỏ qua warp, resize thay thế")
        return art.resize(output_size, Image.LANCZOS)


def warp_art_to_zone(art: Image.Image, warp_pts: list,
                     canvas_w: int, canvas_h: int) -> Image.Image:
    """
    Warp art vào vùng 4 điểm % trên canvas.
    warp_pts: [[x%,y%] × 4] theo thứ tự TL, TR, BR, BL
    """
    w, h = art.size
    src = [[0, 0], [w, 0], [w, h], [0, h]]
    dst = [[p[0] * canvas_w, p[1] * canvas_h] for p in warp_pts]
    return perspective_warp(art, src, dst, (canvas_w, canvas_h))


# ── Blend modes ───────────────────────────────────────────────────────────────
def multiply_blend(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Multiply: base × top / 255 — giữ texture vải."""
    return (base.astype(np.float32) * top.astype(np.float32) / 255).astype(np.uint8)


def overlay_blend(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Overlay: multiply khi base < 0.5, screen khi base ≥ 0.5."""
    b = base.astype(np.float32) / 255
    t = top.astype(np.float32)  / 255
    result = np.where(b < 0.5, 2 * b * t, 1 - 2 * (1 - b) * (1 - t))
    return (np.clip(result, 0, 1) * 255).astype(np.uint8)


def screen_blend(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Screen: 1 − (1−base)(1−top) — làm sáng áo tối."""
    b = base.astype(np.float32) / 255
    t = top.astype(np.float32)  / 255
    return (np.clip(1 - (1 - b) * (1 - t), 0, 1) * 255).astype(np.uint8)


# ── Composite art lên mockup ──────────────────────────────────────────────────
def composite_art_on_mockup(
    mockup: Image.Image,
    art: Image.Image,
    config: dict,
    shape: str = None,
    mockup_filename: str = None,
) -> Image.Image:
    """
    Ghép art lên mockup theo config.

    config keys:
        anchor        : (x%, y%) — tâm art trên mockup
        scale         : tỉ lệ kích thước art so với mockup
        scale_by      : 'width' | 'height'
        rotation      : độ xoay chiều kim đồng hồ (mặc định 0)
        blend_mode    : 'multiply' | 'overlay' | 'screen' | 'normal'
        blend_opacity : 0.0–1.0
        shadow_opacity: 0.0–1.0
        use_warp      : True/False
        warp_pts      : [[x%,y%] × 4] hoặc None
        auto_blend    : True (mặc định) | False
        shape_overrides: override config theo shape art
    """
    from config_mockup import resolve_config_for_shape

    mockup = mockup.convert("RGBA")
    mw, mh = mockup.size

    # Auto-detect blend mode từ tên file mockup
    if mockup_filename:
        config = apply_auto_blend_to_config(config, mockup_filename)

    art = process_art(art)

    # Detect shape từ bounding box alpha
    if shape is None:
        shape = detect_art_shape(art)
        info(f"  [SHAPE] {shape}")

    # Merge shape_overrides vào config
    _overrides = config.get("shape_overrides", {})
    if _overrides.get(shape):
        config = resolve_config_for_shape(config, shape)
        info(f"  [SHAPE] override áp dụng cho shape={shape}")

    scale          = config.get("scale",         0.35)
    scale_by       = config.get("scale_by",      "height")
    ax, ay         = config.get("anchor",        (0.5, 0.5))
    rotation       = config.get("rotation",      0)
    blend_mode     = config.get("blend_mode",    "multiply")
    blend_opacity  = config.get("blend_opacity", 0.30)
    shadow_opacity = config.get("shadow_opacity",0.20)
    use_warp       = config.get("use_warp",      False)
    warp_pts       = config.get("warp_pts",      None)

    # Rotation trước khi scale (expand=True giữ toàn bộ nội dung)
    if rotation:
        art = art.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        info(f"  [ROTATE] {rotation}°")

    # Scale art
    if scale_by == "height":
        new_h = int(mh * scale)
        new_w = int(art.width * new_h / art.height)
    else:
        new_w = int(mw * scale)
        new_h = int(art.height * new_w / art.width)
    art = art.resize((new_w, new_h), Image.LANCZOS)

    # Anchor placement
    px = int(ax * mw) - new_w // 2
    py = int(ay * mh) - new_h // 2

    # Warp
    if use_warp and warp_pts:
        info(f"  [WARP]  {len(warp_pts)} điểm → canvas {mw}×{mh}")
        art_layer    = warp_art_to_zone(art, warp_pts, mw, mh)
        use_absolute = True
    else:
        art_layer    = art
        use_absolute = False

    # Blend với texture mockup
    result_np = np.array(mockup.copy())

    if use_absolute:
        art_rgba  = np.array(art_layer.convert("RGBA"))
    else:
        tmp = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
        tmp.paste(art_layer, (px, py), art_layer)
        art_rgba = np.array(tmp)

    art_alpha = art_rgba[:, :, 3:4] / 255.0
    art_rgb   = art_rgba[:, :, :3]
    base_rgb  = result_np[:, :, :3]

    if blend_mode == "multiply":
        blended = multiply_blend(art_rgb, base_rgb)
    elif blend_mode == "overlay":
        blended = overlay_blend(art_rgb, base_rgb)
    elif blend_mode == "screen":
        blended = screen_blend(art_rgb, base_rgb)
    else:
        blended = art_rgb

    info(f"  [BLEND] {blend_mode}  opacity={blend_opacity}  shadow={shadow_opacity}")

    mixed_rgb = (
        blended.astype(np.float32) * blend_opacity +
        art_rgb.astype(np.float32) * (1 - blend_opacity)
    ).astype(np.uint8)

    out_rgb = (
        mixed_rgb.astype(np.float32) * art_alpha +
        base_rgb.astype(np.float32)  * (1 - art_alpha)
    ).astype(np.uint8)

    result_np[:, :, :3] = out_rgb
    result = Image.fromarray(result_np)

    # Shadow overlay
    if shadow_opacity > 0:
        result = apply_shadow_overlay(result, mockup, art_alpha, shadow_opacity)

    return result


def apply_shadow_overlay(
    composited: Image.Image,
    original_mockup: Image.Image,
    art_alpha: np.ndarray,
    shadow_opacity: float = 0.25,
) -> Image.Image:
    """Đè shadow/highlight của áo gốc lên vùng art — giúp nếp gấp hiện tự nhiên."""
    comp_np  = np.array(composited)
    mock_np  = np.array(original_mockup.convert("RGBA"))
    mock_gray = np.mean(mock_np[:, :, :3], axis=2, keepdims=True) / 255.0

    shadow_mask    = np.clip((0.5 - mock_gray) * 2, 0, 1)
    highlight_mask = np.clip((mock_gray - 0.5) * 2, 0, 1)

    out = comp_np.copy().astype(np.float32)
    out[:, :, :3] -= shadow_mask    * art_alpha * shadow_opacity * 255
    out[:, :, :3] += highlight_mask * art_alpha * shadow_opacity * 0.5 * 255
    out[:, :, :3]  = np.clip(out[:, :, :3], 0, 255)
    return Image.fromarray(out.astype(np.uint8))


# ── Auto blend mode theo tone màu áo ─────────────────────────────────────────
_TONE_KEYWORDS: dict[str, list[str]] = {
    "light": [
        "white", "cream", "ivory", "light", "pale", "sand", "beige",
        "natural", "off_white", "offwhite", "snow", "chalk", "linen",
        "vanilla", "pearl", "oatmeal", "ash",
        "trang", "kem", "nhat",
    ],
    "dark": [
        "black", "dark", "navy", "charcoal", "midnight", "onyx",
        "ebony", "jet", "forest", "hunter", "deep", "graphite",
        "slate", "indigo", "maroon", "burgundy", "wine",
        "den", "toi", "dam",
    ],
}


def detect_mockup_tone(filename: str) -> str:
    """Nhận diện tone màu áo từ tên file. Trả về: 'light' | 'dark' | 'mid'"""
    stem = Path(filename).stem.lower().replace("-", " ").replace("_", " ")
    for tone, keywords in _TONE_KEYWORDS.items():
        for kw in keywords:
            if f" {kw} " in f" {stem} ":
                return tone
    return "mid"


def auto_blend_mode_from_filename(
    filename: str,
    light_mode: str = "multiply",
    mid_mode:   str = "multiply",
    dark_mode:  str = "screen",
) -> str:
    """Chọn blend mode tự động theo tone màu áo trong tên file mockup."""
    tone = detect_mockup_tone(filename)
    mode = {"light": light_mode, "mid": mid_mode, "dark": dark_mode}[tone]
    info(f"  [BLEND] auto '{Path(filename).name}' → tone={tone} → {mode}")
    return mode


def apply_auto_blend_to_config(config: dict, mockup_filename: str) -> dict:
    """Ghi đè blend_mode tự động. Bỏ qua nếu config có 'auto_blend': False."""
    if not config.get("auto_blend", True):
        return config
    mode = auto_blend_mode_from_filename(mockup_filename)
    return {**config, "blend_mode": mode}


# ── Detect shape của art ──────────────────────────────────────────────────────
def detect_art_shape(img: Image.Image) -> str:
    """
    Phân loại art: 'portrait' | 'square' | 'landscape'.
    Dùng bounding box vùng có alpha > 10 (tránh canvas padding sai shape).

    Ngưỡng:
      landscape : ratio > 1.3
      portrait  : ratio < 0.77
      square    : 0.77 – 1.3
    """
    if img.mode in ("RGBA", "LA"):
        alpha = np.array(img.getchannel("A"))
        rows  = np.any(alpha > 10, axis=1)
        cols  = np.any(alpha > 10, axis=0)
        if rows.any() and cols.any():
            row_idx   = np.where(rows)[0]
            col_idx   = np.where(cols)[0]
            content_h = int(row_idx[-1]) - int(row_idx[0]) + 1
            content_w = int(col_idx[-1]) - int(col_idx[0]) + 1
            ratio = content_w / content_h
            if ratio < 0.77:  return "portrait"
            if ratio > 1.30:  return "landscape"
            return "square"
    w, h  = img.size
    ratio = w / h
    if ratio < 0.77:  return "portrait"
    if ratio > 1.30:  return "landscape"
    return "square"
