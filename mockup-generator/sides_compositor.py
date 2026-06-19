"""
sides_compositor.py — Ghép 2 art (front + back) lên mockup 2 mặt
==================================================================
Input:
  - arts_front_folder/ : folder art mặt trước (PNG có alpha)
  - arts_back_folder/  : folder art mặt lưng  (PNG có alpha)
  - mockup_folder/     : folder ảnh mockup chứa cả 2 mặt áo

Output:
  - output_dir/{config_key}/{mockup_stem}__{front_stem}__{back_stem}_{config}.png

Ghép theo thứ tự: back lên mockup gốc → front lên kết quả
(front đè lên back nếu có overlap).

Pair front/back theo tên file (xóa từ "front"/"back" rồi so sánh),
fallback: pair theo thứ tự.

Quy tắc map mockup → config:
  sides_A1.jpg, sides_A2.jpg → "sides_A"
  sides_B1.jpg               → "sides_B"

Dùng:
  # 1 cặp art + 1 mockup
  python mockup-generator/sides_compositor.py \\
    --mockup mockup_photos/sides_A1.jpg \\
    --art-front arts/mickey.png --art-back arts/mickey_back.png \\
    --config sides_A

  # Batch (paired theo tên)
  python mockup-generator/sides_compositor.py \\
    --mockup mockup_photos/sides_A1.jpg \\
    --arts-front arts_front/ --arts-back arts_back/ --config sides_A

  # Multi-pose: folder art × folder mockup
  python mockup-generator/sides_compositor.py \\
    --multi-pose \\
    --mockup-folder sides_photos/ \\
    --arts-front arts_front/ --arts-back arts_back/

  # Liệt kê configs
  python mockup-generator/sides_compositor.py --list-configs
"""

import sys
import re
import time
import argparse
from collections import defaultdict
from pathlib import Path

from PIL import Image

from core_compositor import (
    C, ts, info, warn, ckpt, done, err,
    composite_art_on_mockup, process_art, get_art_files, save_image,
    SUPPORTED_EXTS,
)
from config_mockup import SIDES_CONFIGS, get_sides_config


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_MOCKUP_FOLDER      = Path("sides_photos")
DEFAULT_ARTS_FRONT_FOLDER  = Path("extracted_art/arts_rembg2")
DEFAULT_ARTS_BACK_FOLDER   = Path("extracted_art/arts_rembg2")
DEFAULT_OUTPUT_DIR         = Path("output/sides")
DEFAULT_CONFIG_KEY         = "sides_A"
DEFAULT_FORMAT             = "PNG"


# ── Composite 2 art ───────────────────────────────────────────────────────────
def composite_two_arts(
    mockup_path: Path,
    art_front_path: Path,
    art_back_path: Path,
    cfg_front: dict,
    cfg_back: dict,
) -> Image.Image:
    """
    Ghép back lên mockup gốc, rồi ghép front lên kết quả.
    Shape mỗi art được detect độc lập.
    """
    mockup    = Image.open(mockup_path).convert("RGBA")
    art_front = process_art(Image.open(art_front_path))
    art_back  = process_art(Image.open(art_back_path))

    result = composite_art_on_mockup(mockup, art_back, cfg_back,
                                     mockup_filename=mockup_path.name)
    result = composite_art_on_mockup(result, art_front, cfg_front,
                                     mockup_filename=mockup_path.name)
    return result


# ── Single shot ───────────────────────────────────────────────────────────────
def process_sides_single(
    mockup_path: Path,
    art_front_path: Path,
    art_back_path: Path,
    config_key: str,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
) -> dict:
    """
    Ghép 1 cặp art front+back lên 1 mockup.
    Trả về dict: {mockup, art_front, art_back, config, status, output, elapsed}.
    """
    cfg = get_sides_config(config_key)

    out_name = (f"{mockup_path.stem}__{art_front_path.stem}__{art_back_path.stem}"
                f"_{config_key}.{output_format.lower()}")
    out_path = output_dir / out_name

    if skip_done and out_path.exists():
        info(f"  [SIDES] {out_name} — skip")
        return {"mockup": mockup_path.name,
                "art_front": art_front_path.name, "art_back": art_back_path.name,
                "config": config_key, "status": "skipped",
                "output": str(out_path), "elapsed": 0.0}

    info(f"  [SIDES] front={art_front_path.name}  back={art_back_path.name}")
    info(f"  [SIDES] mockup={mockup_path.name}  config={config_key}")
    t0 = time.time()
    try:
        result  = composite_two_arts(
            mockup_path, art_front_path, art_back_path,
            cfg["config_front"], cfg["config_back"],
        )
        save_image(result, out_path, output_format)
        elapsed = round(time.time() - t0, 1)
        done(f"  [SIDES] Saved ({elapsed}s) → {out_path.name}")
        return {"mockup": mockup_path.name,
                "art_front": art_front_path.name, "art_back": art_back_path.name,
                "config": config_key, "status": "done",
                "output": str(out_path), "elapsed": elapsed}
    except Exception as e:
        err(f"  [SIDES] Lỗi: {e}")
        return {"mockup": mockup_path.name,
                "art_front": art_front_path.name, "art_back": art_back_path.name,
                "config": config_key, "status": "failed",
                "output": "", "elapsed": 0.0}


# ── Pair front + back ─────────────────────────────────────────────────────────
def match_pairs(fronts: list, backs: list) -> list:
    """Pair front + back theo tên (xóa từ khoá front/back), fallback theo thứ tự."""
    def clean(p: Path) -> str:
        s = p.stem.lower()
        for kw in ["_front", "_back", "-front", "-back", "_f_", "_b_"]:
            s = s.replace(kw, "")
        return s.strip("_-")

    back_map = {clean(b): b for b in backs}
    pairs, unmatched = [], []

    for f in fronts:
        key = clean(f)
        if key in back_map:
            pairs.append((f, back_map[key]))
        else:
            unmatched.append(f)

    used   = {b for _, b in pairs}
    remain = [b for b in backs if b not in used]
    for f, b in zip(unmatched, remain):
        pairs.append((f, b))
        warn(f"  [PAIR] fallback: {f.name} + {b.name}")

    info(f"[PAIR] {len(pairs)} cặp art từ {len(fronts)} front + {len(backs)} back")
    return pairs


# ── Batch ─────────────────────────────────────────────────────────────────────
def process_sides_batch(
    mockup_path: Path,
    arts_front_folder: Path,
    arts_back_folder: Path,
    config_key: str,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
) -> list:
    """Batch: ghép từng cặp front+back lên 1 mockup."""
    fronts = get_art_files(arts_front_folder)
    backs  = get_art_files(arts_back_folder)
    if not fronts or not backs:
        err("[BATCH] Cần ít nhất 1 art front và 1 art back")
        return []

    pairs = match_pairs(fronts, backs)
    info(f"[BATCH SIDES] {len(pairs)} cặp  config={config_key}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, (af, ab) in enumerate(pairs, 1):
        print(f"\n{'─'*55}")
        info(f"[BATCH SIDES] [{i}/{len(pairs)}]")
        entry = process_sides_single(mockup_path, af, ab, config_key,
                                     output_dir, output_format, skip_done)
        results.append(entry)

    _print_summary("BATCH SIDES", results)
    return results


# ── Auto-detect sides mockup map ──────────────────────────────────────────────
def auto_build_sides_mockup_map(folder: Path) -> dict:
    """
    Map tự động: tên file mockup → sides config key.

    Ưu tiên:
      1. Tên file chứa key đầy đủ: sides_A1.jpg → "sides_A"
      2. Suffix đứng độc lập: A1.jpg            → "sides_A"

    Trả về: {"sides_A": [Path, ...], "sides_B": [...], ...}
    """
    all_files   = sorted([f for f in folder.iterdir() if f.suffix.lower() in SUPPORTED_EXTS])
    keys_sorted = sorted(SIDES_CONFIGS.keys(), key=len, reverse=True)
    key_suffix_map = {
        k: k.split("_")[-1].lower()
        for k in SIDES_CONFIGS if "_" in k
    }

    mapping   = defaultdict(list)
    unmatched = []

    info(f"[MAP] Auto-detect sides config từ {folder}/ ({len(all_files)} file)")

    for f in all_files:
        stem    = f.stem.lower()
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
            warn(f"  [MAP] {f.name} → không khớp config nào — bỏ qua")

    if unmatched:
        warn(f"[MAP] {len(unmatched)} file bị bỏ qua")

    result = dict(mapping)
    if result:
        for k, v in result.items():
            info(f"  [MAP] {k}: {len(v)} mockup — {', '.join(p.name for p in v)}")
    else:
        err("[MAP] Không tìm thấy mockup nào! Kiểm tra tên file.")
    return result


# ── Multi-pose ────────────────────────────────────────────────────────────────
def process_sides_multi_pose(
    mockup_map: dict,
    arts_front_folder: Path,
    arts_back_folder: Path,
    output_dir: Path,
    output_format: str = DEFAULT_FORMAT,
    skip_done: bool = True,
) -> list:
    """
    Multi-pose: mỗi cặp art × mỗi mockup trong group config → 1 output.
    mockup_map: {"sides_A": [mockup1, mockup2, ...], ...}
    """
    fronts = get_art_files(arts_front_folder)
    backs  = get_art_files(arts_back_folder)
    if not fronts or not backs:
        err("[MULTI] Cần ít nhất 1 art front và 1 art back")
        return []

    pairs         = match_pairs(fronts, backs)
    total_mockups = sum(len(v) for v in mockup_map.values())
    info(f"[MULTI SIDES] {len(pairs)} cặp × {total_mockups} mockup "
         f"({len(mockup_map)} group) = {len(pairs) * total_mockups} outputs")

    results = []
    for config_key, mockup_list in mockup_map.items():
        sides_dir = output_dir / config_key
        sides_dir.mkdir(parents=True, exist_ok=True)
        info(f"[MULTI SIDES] Group [{config_key}] — {len(mockup_list)} mockup → {sides_dir}")

        for mockup_path in mockup_list:
            if not mockup_path.exists():
                warn(f"  [MULTI] Không tìm thấy: {mockup_path}")
                continue
            for af, ab in pairs:
                print(f"\n{'─'*55}")
                entry = process_sides_single(mockup_path, af, ab, config_key,
                                             sides_dir, output_format, skip_done)
                results.append(entry)

    _print_summary("MULTI SIDES", results)
    return results


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
        description="2 Sides Compositor — Ghép 2 art lên mockup front+back",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--mockup",         help="Ảnh mockup (single/batch)")
    p.add_argument("--mockup-folder",  help="Folder mockup nhiều pose (multi-pose)")
    p.add_argument("--art-front",      help="Art mặt trước (single)")
    p.add_argument("--art-back",       help="Art mặt lưng  (single)")
    p.add_argument("--arts-front",     default=str(DEFAULT_ARTS_FRONT_FOLDER),
                   help=f"Folder art front (default: {DEFAULT_ARTS_FRONT_FOLDER})")
    p.add_argument("--arts-back",      default=str(DEFAULT_ARTS_BACK_FOLDER),
                   help=f"Folder art back  (default: {DEFAULT_ARTS_BACK_FOLDER})")
    p.add_argument("--config",         default=DEFAULT_CONFIG_KEY,
                   help=f"Config key (default: {DEFAULT_CONFIG_KEY})")
    p.add_argument("--multi-pose",     action="store_true",
                   help="Ghép folder art × folder mockup (auto-detect config)")
    p.add_argument("--output",         default=str(DEFAULT_OUTPUT_DIR),
                   help=f"Folder kết quả (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--format",         default=DEFAULT_FORMAT, choices=["PNG", "JPG", "JPEG"])
    p.add_argument("--redo",           action="store_true", help="Redo output đã có")
    p.add_argument("--list-configs",   action="store_true", help="Liệt kê configs rồi thoát")
    return p.parse_args()


def main():
    args      = _parse_args()
    skip_done = not args.redo

    if args.list_configs:
        print("\n[Sides Configs]")
        for k, v in SIDES_CONFIGS.items():
            fa = v["config_front"]["anchor"]
            ba = v["config_back"]["anchor"]
            print(f"  {k}: front anchor={fa}  back anchor={ba}")
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Multi-pose
    if args.multi_pose or args.mockup_folder:
        if not args.mockup_folder:
            err("[MULTI] Cần --mockup-folder")
            sys.exit(1)
        mockup_map = auto_build_sides_mockup_map(Path(args.mockup_folder))
        if not mockup_map:
            err("[MULTI] Không tìm thấy mockup khớp config")
            sys.exit(1)
        process_sides_multi_pose(
            mockup_map,
            Path(args.arts_front), Path(args.arts_back),
            output_dir, args.format, skip_done,
        )
        return

    if not args.mockup:
        err("Cần --mockup (hoặc --mockup-folder cho multi-pose)")
        sys.exit(1)

    mockup_path = Path(args.mockup)
    if not mockup_path.exists():
        err(f"Không tìm thấy: {mockup_path}")
        sys.exit(1)

    # Single
    if args.art_front and args.art_back:
        process_sides_single(
            mockup_path,
            Path(args.art_front), Path(args.art_back),
            args.config, output_dir, args.format, skip_done,
        )
        return

    # Batch
    process_sides_batch(
        mockup_path,
        Path(args.arts_front), Path(args.arts_back),
        args.config, output_dir, args.format, skip_done,
    )


if __name__ == "__main__":
    main()
