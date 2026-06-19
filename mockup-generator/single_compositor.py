"""
single_compositor.py — Ghép 1 art lên mockup người mẫu (1 mặt)
================================================================
Input:
  - arts_folder/  : folder chứa art đã xóa phông (PNG có alpha)
  - mockup_folder/: folder chứa ảnh mockup người mẫu

Output:
  - output_dir/{listing_id}_{mockup_stem}_single.png

Quy tắc map tên mockup → config key:
  pose_A1.jpg, white_pose_A.jpg  → config "pose_A"
  pose_B1.jpg                    → config "pose_B"
  (nếu không khớp → fallback config đầu tiên)

Dùng:
  # 1 art + 1 mockup
  python mockup-generator/single_compositor.py \\
    --mockup mockup_photos/pose_A1.jpg --art arts/mickey.png --config pose_A

  # Batch: folder art × 1 mockup
  python mockup-generator/single_compositor.py \\
    --mockup mockup_photos/pose_A1.jpg --arts-folder arts/ --config pose_A

  # Multi-pose: folder art × folder mockup (auto-detect config theo tên file)
  python mockup-generator/single_compositor.py \\
    --multi-pose --mockup-folder mockup_photos/ --arts-folder arts/

  # Liệt kê configs
  python mockup-generator/single_compositor.py --list-configs
"""

import sys
import time
import argparse
import re
import csv
from collections import defaultdict
from pathlib import Path

from PIL import Image

from core_compositor import (
    C, ts, info, warn, ckpt, done, err,
    composite_art_on_mockup, get_art_files, save_image,
)
from config_mockup import SINGLE_CONFIGS, get_single_config


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_MOCKUP_FOLDER = Path("D:\\Intern\\Image\\mockup_photos\\Single")
DEFAULT_ARTS_FOLDER   = Path("extracted_art/arts_rembg2")
DEFAULT_OUTPUT_DIR    = Path("output/single")
DEFAULT_CONFIG_KEY    = "pose_A"
DEFAULT_FORMAT        = "PNG"


# ── [TILT ADJUST — commented out — mở khi tilt detection hoàn chỉnh] ─────────
# Đọc crop_coords.csv để bù trừ độ nghiêng tự nhiên của art trước khi ghép.
# Logic: effective_rotation = config_rotation - tilt
#   - tilt > 0 : art nghiêng CW  → cần xoay CCW để thẳng rồi áp config rotation
#   - tilt < 0 : art nghiêng CCW → cần xoay CW để thẳng rồi áp config rotation
#
# Để kích hoạt:
#   1. Bỏ comment toàn bộ block này
#   2. Thêm param  tilt_map: dict = None  vào process_single / process_batch / process_multi_pose
#   3. Bỏ comment block [TILT] trong process_single
#   4. Bỏ comment block [TILT] trong main() và truyền tilt_map qua các hàm

# import csv
#
# DEFAULT_CROP_COORDS = Path("crop_coords.csv")
#
#
# def load_tilt_map(csv_path: Path) -> dict:
#     """Đọc crop_coords.csv → {listing_id: tilt_float}."""
#     tilt_map = {}
#     if not csv_path.exists():
#         warn(f"[TILT] Không tìm thấy {csv_path} — bỏ qua tilt adjustment")
#         return tilt_map
#     with open(csv_path, newline="", encoding="utf-8") as f:
#         reader = csv.DictReader(f)
#         for row in reader:
#             lid  = str(row.get("listing_id", "")).strip()
#             tilt = str(row.get("tilt", "")).strip()
#             if lid and tilt:
#                 try:
#                     tilt_map[lid] = float(tilt)
#                 except ValueError:
#                     warn(f"[TILT] lid={lid}: tilt không phải số ({tilt!r})")
#     info(f"[TILT] Đã load {len(tilt_map)} entry từ {csv_path.name}")
#     return tilt_map
#
#
# def extract_lid_from_stem(stem: str) -> str:
#     """Tách listing_id từ stem: '1234_art' → '1234', '1234_art_back' → '1234'."""
#     for suffix in ("_art_back", "_art"):
#         if stem.endswith(suffix):
#             return stem[: -len(suffix)]
#     return stem
# ─────────────────────────────────────────────────────────────────────────────


# ── HeyEtsy CSV output ────────────────────────────────────────────────────────
HEYETSY_SOURCE_CSV = Path("heyetsy_image_urls.csv")
HEYETSY_OUTPUT_CSV = Path("output/replace_mockup.csv")
HEYETSY_SHOP_NAME  = "Replace Mockup"
MAX_IMAGES         = 20


# ── Single shot ───────────────────────────────────────────────────────────────
def process_single(
    mockup_path: Path,
    art_path: Path,
    config_key: str,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
) -> dict:
    """
    Ghép 1 art lên 1 mockup theo config_key.
    Trả về dict: {mockup, art, config, status, output, elapsed}.
    """
    config   = get_single_config(config_key)

    # Tách listing_id từ stem (vd: "1739309808_art" → "1739309808")
    stem = art_path.stem
    lid  = stem[:-4] if stem.endswith("_art") else stem

    # ── [TILT] Bù trừ độ nghiêng art — commented out ─────────────────────────
    # Thêm param  tilt_map: dict = None  vào def process_single(...) trước khi dùng.
    #
    # tilt = tilt_map.get(lid, 0.0) if tilt_map else 0.0
    # if tilt != 0.0:
    #     config = dict(config)  # copy để không mutate config cache
    #     config["rotation"] = config.get("rotation", 0) - tilt
    #     info(f"  [TILT] lid={lid}  tilt={tilt:.1f}°  "
    #          f"→ effective_rotation={config['rotation']:.1f}°")
    # ─────────────────────────────────────────────────────────────────────────

    out_name = f"{lid}_{mockup_path.stem}_single.{output_format.lower()}"
    out_path = output_dir / out_name

    if skip_done and out_path.exists():
        info(f"  [SINGLE] {out_name} — skip")
        return {"mockup": mockup_path.name, "art": art_path.name,
                "config": config_key, "status": "skipped", "output": str(out_path), "elapsed": 0.0}

    info(f"  [SINGLE] {art_path.name} × {mockup_path.name}  [{config_key}]")
    t0 = time.time()
    try:
        mockup = Image.open(mockup_path)
        art    = Image.open(art_path)
        result = composite_art_on_mockup(
            mockup=mockup, art=art, config=config,
            mockup_filename=mockup_path.name,
        )
        save_image(result, out_path, output_format)
        elapsed = round(time.time() - t0, 1)
        done(f"  [SINGLE] Saved ({elapsed}s) → {out_path.name}")
        return {"mockup": mockup_path.name, "art": art_path.name,
                "config": config_key, "status": "done", "output": str(out_path), "elapsed": elapsed}
    except Exception as e:
        err(f"  [SINGLE] Lỗi: {e}")
        return {"mockup": mockup_path.name, "art": art_path.name,
                "config": config_key, "status": "failed", "output": "", "elapsed": 0.0}


# ── Batch ─────────────────────────────────────────────────────────────────────
def process_batch(
    mockup_path: Path,
    arts_folder: Path,
    config_key: str,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
    listing_ids: list = None,
) -> list:
    """Ghép tất cả art trong folder lên 1 mockup."""
    arts = get_art_files(arts_folder)
    if not arts:
        err(f"[BATCH] Không tìm thấy art nào trong {arts_folder}")
        return []

    if listing_ids:
        arts = filter_arts_by_ids(arts, listing_ids)
        skip_done = False   # chỉ định ID cụ thể → luôn redo
    if not arts:
        warn("[BATCH] Không có art nào sau khi lọc listing_ids")
        return []

    info(f"[BATCH] {len(arts)} art × mockup={mockup_path.name}  config={config_key}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, art_path in enumerate(arts, 1):
        print(f"\n{'─'*55}")
        info(f"[BATCH] [{i}/{len(arts)}] {art_path.name}")
        entry = process_single(mockup_path, art_path, config_key,
                               output_dir, output_format, skip_done)
        results.append(entry)

    _print_summary("BATCH", results)
    return results


# ── Multi-pose ────────────────────────────────────────────────────────────────
def process_multi_pose(
    mockup_map: dict,
    arts_folder: Path,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
    listing_ids: list = None,
) -> list:
    """
    Ghép tất cả art lên toàn bộ mockup, group theo config key.
    mockup_map: {"pose_A": [Path, ...], "pose_B": [...], ...}

    Output: output_dir/{listing_id}_{mockup_stem}_single.png  (flat, không subfolder)
    """
    arts = get_art_files(arts_folder)
    if not arts:
        err(f"[MULTI] Không tìm thấy art nào trong {arts_folder}")
        return []

    if listing_ids:
        arts = filter_arts_by_ids(arts, listing_ids)
        skip_done = False
    if not arts:
        warn("[MULTI] Không có art nào sau khi lọc listing_ids")
        return []

    total_mockups = sum(len(v) for v in mockup_map.values())
    total         = len(arts) * total_mockups
    info(f"[MULTI] {len(arts)} art × {total_mockups} mockup "
         f"({len(mockup_map)} config group) = {total} outputs")

    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for config_key, mockup_paths in mockup_map.items():
        info(f"[MULTI] Group [{config_key}] — {len(mockup_paths)} mockup")

        for mockup_path in mockup_paths:
            if not mockup_path.exists():
                warn(f"  [MULTI] Không tìm thấy mockup: {mockup_path}")
                continue
            for art_path in arts:
                print(f"\n{'─'*55}")
                entry = process_single(mockup_path, art_path, config_key,
                                       output_dir, output_format, skip_done)
                results.append(entry)

    _print_summary("MULTI", results)
    return results


# ── Auto-detect mockup → config map ──────────────────────────────────────────
def auto_build_mockup_map(mockup_folder: Path) -> dict:
    """
    Map tự động: tên file mockup → config key.

    Ưu tiên:
      1. Tên file chứa key đầy đủ: pose_B1.jpg  → "pose_B"
      2. Suffix letter đứng độc lập: B1.jpg      → "pose_B"
      3. Fallback: config đầu tiên

    Trả về: {"pose_A": [Path, ...], "pose_B": [...], ...}
    """
    exts  = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted([f for f in mockup_folder.iterdir() if f.suffix.lower() in exts])

    keys_sorted    = sorted(SINGLE_CONFIGS.keys(), key=len, reverse=True)
    key_suffix_map = {
        k: k.split("_")[-1].lower()
        for k in SINGLE_CONFIGS if "_" in k
    }

    mapping   = defaultdict(list)
    unmatched = []

    info(f"[MAP] Auto-detect config map từ {mockup_folder}/ ({len(files)} file)")

    for f in files:
        stem = f.stem.lower()
        matched = None

        for key in keys_sorted:
            if key.lower() in stem:
                matched = key
                break

        if matched is None:
            for key in keys_sorted:
                suffix = key_suffix_map.get(key)
                if suffix and re.search(r'(?<![a-z])' + re.escape(suffix) + r'(?![a-z])', stem):
                    matched = key
                    break

        if matched:
            mapping[matched].append(f)
            info(f"  [MAP] {f.name} → '{matched}'")
        else:
            unmatched.append(f)
            warn(f"  [MAP] {f.name} → không khớp config nào")

    if unmatched and keys_sorted:
        fallback = list(SINGLE_CONFIGS.keys())[0]
        warn(f"  [MAP] {len(unmatched)} file fallback → '{fallback}'")
        for f in unmatched:
            mapping[fallback].append(f)

    if not mapping:
        warn("[MAP] Không tìm thấy mockup nào trong folder")
    else:
        for k, v in mapping.items():
            info(f"  [MAP] {k}: {len(v)} mockup — {', '.join(p.name for p in v)}")

    return dict(mapping)


# ── Filter arts by listing IDs ────────────────────────────────────────────────
def filter_arts_by_ids(arts: list, listing_ids: list) -> list:
    """
    Lọc danh sách art theo listing IDs.

    Match (theo thứ tự ưu tiên):
      - stem == "{lid}_art"      → {lid}_art.png  (convention remove_background.py)
      - stem == "{lid}"          → {lid}.png       (tên đơn giản)
      - stem.startswith("{lid}_")→ {lid}_*.png     (có suffix bất kỳ)

    Trả về list[Path] chỉ gồm các art khớp, giữ nguyên thứ tự gốc.
    """
    id_set = {str(i) for i in listing_ids}
    result = []
    not_found = set(id_set)

    for art_path in arts:
        stem = art_path.stem
        for lid in id_set:
            if stem == f"{lid}_art" or stem == lid or stem.startswith(f"{lid}_"):
                result.append(art_path)
                not_found.discard(lid)
                break

    if not_found:
        warn(f"[IDS] Không tìm thấy art cho: {', '.join(sorted(not_found))}")
    info(f"[IDS] Lọc theo listing_ids: {len(result)}/{len(arts)} art khớp")
    return result


# ── HeyEtsy CSV writer ────────────────────────────────────────────────────────
def _load_source_csv(csv_path: Path) -> dict:
    """Đọc source CSV → {listing_id: {"title": ..., "tags": ...}}."""
    data = {}
    if not csv_path.exists():
        warn(f"[CSV] Không tìm thấy source CSV: {csv_path}")
        return data
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lid = str(row.get("listing_id", "")).strip()
            if lid:
                data[lid] = {
                    "title": row.get("title", ""),
                    "tags":  row.get("tags",  ""),
                }
    info(f"[CSV] Source: {len(data)} listing từ {csv_path.name}")
    return data


def write_heyetsy_csv(
    output_dir: Path,
    source_csv: Path = HEYETSY_SOURCE_CSV,
    out_csv:    Path = HEYETSY_OUTPUT_CSV,
    shop_name:  str  = HEYETSY_SHOP_NAME,
):
    """
    Quét output_dir tìm *_single.* → gom theo listing_id →
    ghi CSV format heyetsy_image_urls.

    Columns: shop_name, listing_id, title, tags, image_1..image_20
      - shop_name = "Replace Mockup" (constant)
      - title/tags clone từ source_csv theo listing_id
      - image_1..N = đường dẫn tuyệt đối đến mockup file (sắp xếp alphabetical)
    """
    img_exts = {".png", ".jpg", ".jpeg"}
    singles  = sorted([
        f for f in output_dir.iterdir()
        if f.is_file() and "_single" in f.stem and f.suffix.lower() in img_exts
    ])
    if not singles:
        warn(f"[CSV] Không tìm thấy file *_single.* trong {output_dir}")
        return

    # Gom file theo listing_id (tiền tố số đứng đầu: "1739309808_pose_..." → "1739309808")
    groups: dict = {}
    for f in singles:
        m = re.match(r'^(\d+)_', f.stem)
        if m:
            groups.setdefault(m.group(1), []).append(f)

    info(f"[CSV] {len(groups)} listing_id, {len(singles)} mockup file")
    source   = _load_source_csv(source_csv)
    img_cols = [f"image_{i}" for i in range(1, MAX_IMAGES + 1)]
    headers  = ["shop_name", "listing_id", "title", "tags"] + img_cols
    no_source = []

    # Sắp xếp theo thứ tự source CSV, IDs không có trong source thêm cuối
    ordered_lids  = [lid for lid in source if lid in groups]
    remaining_lids = [lid for lid in sorted(groups) if lid not in source]
    write_order   = ordered_lids + remaining_lids

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for lid in write_order:
            files = groups[lid]
            meta  = source.get(lid, {})
            if not meta:
                no_source.append(lid)
            images = [str(p.resolve()) for p in files[:MAX_IMAGES]]
            images += [""] * (MAX_IMAGES - len(images))
            writer.writerow([shop_name, lid,
                             meta.get("title", ""), meta.get("tags", ""),
                             *images])

    if no_source:
        warn(f"[CSV] Không tìm thấy title/tags cho: {', '.join(no_source)}")
    done(f"[CSV] {len(groups)} dòng → {out_csv}")


# ── Summary ───────────────────────────────────────────────────────────────────
def _print_summary(label: str, results: list):
    done_cnt   = sum(1 for r in results if r["status"] == "done")
    skip_cnt   = sum(1 for r in results if r["status"] == "skipped")
    failed_cnt = sum(1 for r in results if r["status"] == "failed")
    avg = (
        sum(r["elapsed"] for r in results if r["status"] == "done") / done_cnt
        if done_cnt else 0
    )
    print(f"\n{'═'*55}")
    done(f"[{label}] ✓ {done_cnt}  ⟳ {skip_cnt}  ✗ {failed_cnt}  (~{avg:.1f}s/ảnh)")


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args():
    p = argparse.ArgumentParser(
        description="Single Compositor — Ghép 1 art lên mockup người mẫu",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--mockup",        help="File mockup (jpg/png) — single/batch mode")
    p.add_argument("--art",           help="File art (single shot)")
    p.add_argument("--arts-folder",   default=str(DEFAULT_ARTS_FOLDER),
                   help=f"Folder art batch (default: {DEFAULT_ARTS_FOLDER})")
    p.add_argument("--config",        default=DEFAULT_CONFIG_KEY,
                   help=f"Config key (default: {DEFAULT_CONFIG_KEY})")
    p.add_argument("--multi-pose",    action="store_true",
                   help="Ghép tất cả art × tất cả mockup (auto-detect config theo tên file)")
    p.add_argument("--mockup-folder", default=str(DEFAULT_MOCKUP_FOLDER),
                   help=f"Folder mockup khi dùng --multi-pose (default: {DEFAULT_MOCKUP_FOLDER})")
    p.add_argument("--output",        default=str(DEFAULT_OUTPUT_DIR),
                   help=f"Folder kết quả (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--format",        default=DEFAULT_FORMAT, choices=["PNG", "JPG", "JPEG"])
    p.add_argument("--redo",          action="store_true", help="Redo output đã có")
    p.add_argument("--listing-ids",   nargs="+", metavar="ID",
                   help="Chỉ ghép art khớp với listing ID (tự động redo, hỗ trợ batch và multi-pose)")
    p.add_argument("--list-configs",  action="store_true", help="Liệt kê configs rồi thoát")
    p.add_argument("--source-csv",   default=str(HEYETSY_SOURCE_CSV), metavar="CSV",
                   help=f"CSV nguồn lấy title/tags (default: {HEYETSY_SOURCE_CSV})")
    p.add_argument("--heyetsy-csv",  default=str(HEYETSY_OUTPUT_CSV), metavar="CSV",
                   help=f"Output CSV heyetsy (default: {HEYETSY_OUTPUT_CSV})")
    p.add_argument("--no-csv",       action="store_true",
                   help="Bỏ qua bước ghi CSV sau khi ghép")
    # [TILT] Bỏ comment dòng dưới khi mở tilt adjustment:
    # p.add_argument("--crop-coords",   default=str(DEFAULT_CROP_COORDS), metavar="CSV",
    #                help=f"Path đến crop_coords.csv (default: {DEFAULT_CROP_COORDS})")
    return p.parse_args()


def main():
    args       = _parse_args()
    output_dir = Path(args.output)
    skip_done  = not args.redo
    listing_ids = args.listing_ids or None

    # ── [TILT] Load tilt map — commented out ──────────────────────────────────
    # tilt_map = load_tilt_map(Path(args.crop_coords))
    # Sau đó truyền  tilt_map=tilt_map  vào:
    #   process_single(...)    — thêm param tilt_map: dict = None
    #   process_batch(...)     — thêm param tilt_map: dict = None, truyền xuống process_single
    #   process_multi_pose(...)— thêm param tilt_map: dict = None, truyền xuống process_single
    # ─────────────────────────────────────────────────────────────────────────

    if args.list_configs:
        print("\n[Single Configs]")
        for k, v in SINGLE_CONFIGS.items():
            print(f"  {k}: anchor={v.get('anchor')}  scale={v.get('scale')}  "
                  f"blend={v.get('blend_mode')}  rotation={v.get('rotation', 0)}°")
        return

    if args.multi_pose:
        mockup_folder = Path(args.mockup_folder)
        if not mockup_folder.is_dir():
            err(f"[MULTI] Không tìm thấy folder mockup: {mockup_folder}")
            sys.exit(1)
        arts_folder = Path(args.arts_folder)
        if not arts_folder.is_dir():
            err(f"[MULTI] Không tìm thấy folder art: {arts_folder}")
            sys.exit(1)
        mockup_map = auto_build_mockup_map(mockup_folder)
        process_multi_pose(mockup_map, arts_folder, output_dir,
                           args.format, skip_done, listing_ids)
        if not args.no_csv:
            print(f"\n{'═'*55}")
            write_heyetsy_csv(output_dir, Path(args.source_csv), Path(args.heyetsy_csv))
        return

    if not args.mockup:
        err("Cần --mockup (hoặc dùng --multi-pose)")
        sys.exit(1)

    mockup_path = Path(args.mockup)
    if not mockup_path.exists():
        err(f"Không tìm thấy mockup: {mockup_path}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    if args.art:
        art_path = Path(args.art)
        if not art_path.exists():
            err(f"Không tìm thấy art: {art_path}")
            sys.exit(1)
        process_single(mockup_path, art_path, args.config, output_dir, args.format, skip_done)
        if not args.no_csv:
            print(f"\n{'═'*55}")
            write_heyetsy_csv(output_dir, Path(args.source_csv), Path(args.heyetsy_csv))
        return

    process_batch(mockup_path, Path(args.arts_folder), args.config,
                  output_dir, args.format, skip_done, listing_ids)
    if not args.no_csv:
        print(f"\n{'═'*55}")
        write_heyetsy_csv(output_dir, Path(args.source_csv), Path(args.heyetsy_csv))


if __name__ == "__main__":
    main()
