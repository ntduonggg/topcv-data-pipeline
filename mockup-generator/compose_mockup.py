"""
compose_mockup.py — Batch ghép art lên mockup templates (bước cuối pipeline)
=============================================================================
Input:
  - extract_log.csv         : listing_id, front_output, back_output, front_status, back_status
                              (output của remove_background.py)
  - mockup_photos/          : folder ảnh mockup người mẫu (single mode)
  - sides_photos/           : folder ảnh mockup 2 mặt (sides/auto mode)

Pipeline mỗi listing:
  1. Đọc extract_log.csv → lấy path art front/back đã xóa phông
  2. Với mỗi mockup trong folder:
     - single mode : ghép front art lên mockup → output/{listing_id}/{mockup}_{lid}.png
     - sides mode  : ghép front+back art lên mockup 2 mặt
     - auto mode   : listing có back → sides; không có → single
  3. Lưu log → compose_log.csv

Dùng:
  python mockup-generator/compose_mockup.py --mockup-folder mockup_photos/
  python mockup-generator/compose_mockup.py \\
    --mockup-folder mockup_photos/ --sides-folder sides_photos/ --mode auto
  python mockup-generator/compose_mockup.py --mockup-folder mockup_photos/ --config pose_B --limit 5
  python mockup-generator/compose_mockup.py --mockup-folder mockup_photos/ --listing-ids 12345 67890
  python mockup-generator/compose_mockup.py --list-configs
"""

import os
import sys
import time
import signal
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from PIL import Image

# Fix Unicode output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Thêm thư mục script vào sys.path để import compositor modules
_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from core_compositor import (
    C, ts, info, warn, ckpt, done, err,
    composite_art_on_mockup, process_art, save_image, SUPPORTED_EXTS,
)
from config_mockup import (
    SINGLE_CONFIGS, SIDES_CONFIGS,
    get_single_config, get_sides_config,
    list_configs,
)
from single_compositor import auto_build_mockup_map
from sides_compositor import auto_build_sides_mockup_map, composite_two_arts


# ── Config ────────────────────────────────────────────────────────────────────
EXTRACT_LOG      = "extract_log.csv"
ARTS_DIR         = Path("extracted_art/arts_rembg2")
MOCKUP_FOLDER    = Path("mockup_photos")
SIDES_FOLDER     = None                      # None → bỏ qua sides
OUTPUT_DIR       = Path("output/mockups")
COMPOSE_LOG      = "output/compose_log.csv"
CHECKPOINT_EVERY = 20


# ── Load extract log ──────────────────────────────────────────────────────────
def load_extract_log(path: str = EXTRACT_LOG) -> pd.DataFrame:
    """
    Đọc extract_log.csv (output của remove_background.py).
    Lọc các listing có front_status == 'done'.
    Trả về DataFrame với cột: listing_id, front_output, back_output.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Không tìm thấy {path} — chạy remove_background.py trước."
        )
    df = pd.read_csv(path, dtype=str).fillna("")
    required = {"listing_id", "front_status", "front_output"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"extract_log.csv thiếu cột: {missing}")

    before = len(df)
    df = df[df["front_status"] == "done"]
    skipped = before - len(df)
    if skipped:
        warn(f"[LOG] Bỏ {skipped} listing chưa extract xong (front_status ≠ 'done')")

    info(f"[LOG] Loaded {len(df)} listings từ {path}")
    return df


def _scan_arts_dir(arts_dir: Path) -> pd.DataFrame:
    """
    Fallback khi không có extract_log.csv:
    Quét arts_dir, gom {lid}_art.png và {lid}_art_back.png theo listing_id.
    """
    front_map: dict[str, str] = {}
    back_map:  dict[str, str] = {}

    for f in sorted(arts_dir.iterdir()):
        if f.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if f.stem.endswith("_art_back"):
            lid = f.stem[: -len("_art_back")]
            back_map[lid] = str(f)
        elif f.stem.endswith("_art"):
            lid = f.stem[: -len("_art")]
            front_map[lid] = str(f)

    rows = []
    for lid, fpath in sorted(front_map.items()):
        rows.append({
            "listing_id":   lid,
            "front_output": fpath,
            "back_output":  back_map.get(lid, ""),
            "back_status":  "done" if lid in back_map else "",
        })

    warn(f"[LOG] extract_log.csv không tìm thấy — scan {arts_dir}: {len(rows)} listings")
    return pd.DataFrame(rows)


# ── Build work list ───────────────────────────────────────────────────────────
def build_work_list(
    log_df: pd.DataFrame,
    skip_done: bool = True,
    listing_ids: Optional[list] = None,
) -> list:
    """
    Chuyển DataFrame thành list[dict] để xử lý.
    Kiểm tra file art thực sự tồn tại.
    """
    if listing_ids:
        id_set  = set(listing_ids)
        log_df  = log_df[log_df["listing_id"].isin(id_set)]
        missing = id_set - set(log_df["listing_id"])
        if missing:
            warn(f"[WORK] Không tìm thấy trong log: {', '.join(sorted(missing))}")
        info(f"[WORK] --listing-ids: {len(log_df)} listings")

    work = []
    for _, row in log_df.iterrows():
        lid        = row["listing_id"]
        front_path = Path(row.get("front_output", ""))
        back_path  = Path(row.get("back_output",  ""))

        if not front_path.exists():
            warn(f"  [WORK] {lid}: front art không tồn tại ({front_path}) — bỏ qua")
            continue

        has_back = bool(row.get("back_status") == "done") and back_path.exists()

        work.append({
            "listing_id": lid,
            "front_path": front_path,
            "back_path":  back_path if has_back else None,
            "has_back":   has_back,
        })

    info(f"[WORK] {len(work)} listings hợp lệ  "
         f"({sum(1 for x in work if x['has_back'])} có back art)")
    return work


# ── Scan mockup folder ────────────────────────────────────────────────────────
def scan_mockups(folder: Path, label: str = "mockup") -> list:
    if not folder or not folder.exists():
        return []
    files = sorted([f for f in folder.iterdir() if f.suffix.lower() in SUPPORTED_EXTS])
    info(f"[{label.upper()}] {len(files)} file trong {folder}/")
    return files


# ── Compose single listing ────────────────────────────────────────────────────
def _compose_single(
    lid: str,
    front_path: Path,
    mockup_paths: list,
    config_key: str,
    output_dir: Path,
    output_format: str,
    skip_done: bool,
) -> list:
    """Ghép front art lên từng mockup (single mode). Trả về log entries."""
    config  = get_single_config(config_key)
    art_dir = output_dir / lid
    art_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    for mockup_path in mockup_paths:
        out_name = f"{mockup_path.stem}_{lid}.{output_format.lower()}"
        out_path = art_dir / out_name

        if skip_done and out_path.exists():
            info(f"  [SINGLE] {out_name} — skip")
            entries.append(_log_entry(lid, mockup_path, "single", config_key,
                                      "skipped", out_path, 0.0))
            continue

        info(f"  [SINGLE] {mockup_path.name}  config={config_key}")
        t0 = time.time()
        try:
            mockup = Image.open(mockup_path)
            art    = Image.open(front_path)
            result = composite_art_on_mockup(
                mockup=mockup, art=art, config=config,
                mockup_filename=mockup_path.name,
            )
            save_image(result, out_path, output_format)
            elapsed = round(time.time() - t0, 1)
            done(f"  [SINGLE] Saved ({elapsed}s) → {out_path.name}")
            entries.append(_log_entry(lid, mockup_path, "single", config_key,
                                      "done", out_path, elapsed))
        except Exception as e:
            err(f"  [SINGLE] {mockup_path.name}: {e}")
            entries.append(_log_entry(lid, mockup_path, "single", config_key,
                                      "failed", None, 0.0))
    return entries


def _compose_sides(
    lid: str,
    front_path: Path,
    back_path: Path,
    mockup_paths: list,
    config_key: str,
    output_dir: Path,
    output_format: str,
    skip_done: bool,
) -> list:
    """Ghép front+back art lên từng mockup 2 mặt (sides mode). Trả về log entries."""
    cfg = get_sides_config(config_key)
    art_dir = output_dir / lid
    art_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    for mockup_path in mockup_paths:
        out_name = f"{mockup_path.stem}_{lid}_sides.{output_format.lower()}"
        out_path = art_dir / out_name

        if skip_done and out_path.exists():
            info(f"  [SIDES] {out_name} — skip")
            entries.append(_log_entry(lid, mockup_path, "sides", config_key,
                                      "skipped", out_path, 0.0))
            continue

        info(f"  [SIDES] {mockup_path.name}  config={config_key}")
        t0 = time.time()
        try:
            result  = composite_two_arts(
                mockup_path, front_path, back_path,
                cfg["config_front"], cfg["config_back"],
            )
            save_image(result, out_path, output_format)
            elapsed = round(time.time() - t0, 1)
            done(f"  [SIDES] Saved ({elapsed}s) → {out_path.name}")
            entries.append(_log_entry(lid, mockup_path, "sides", config_key,
                                      "done", out_path, elapsed))
        except Exception as e:
            err(f"  [SIDES] {mockup_path.name}: {e}")
            entries.append(_log_entry(lid, mockup_path, "sides", config_key,
                                      "failed", None, 0.0))
    return entries


def _log_entry(lid, mockup_path, mode, config, status, out_path, elapsed) -> dict:
    return {
        "listing_id": lid,
        "mockup":     mockup_path.name,
        "mode":       mode,
        "config":     config,
        "status":     status,
        "output":     str(out_path) if out_path else "",
        "elapsed":    elapsed,
    }


# ── Flush log ─────────────────────────────────────────────────────────────────
def flush_log(log: list, path: str = COMPOSE_LOG):
    if not log:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    pd.DataFrame(log).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"[CKPT] Log flush {len(log)} records → {path}")


def setup_signal(log: list):
    def _handler(sig, frame):
        print()
        warn("Ctrl+C — flush log rồi thoát...")
        flush_log(log)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── Main pipeline ─────────────────────────────────────────────────────────────
def compose_all(
    mockup_folder: Path,
    extract_log_csv: str      = EXTRACT_LOG,
    arts_dir: Path            = ARTS_DIR,
    sides_folder: Optional[Path] = None,
    output_dir: Path          = OUTPUT_DIR,
    mode: str                 = "single",
    config_key: Optional[str] = None,
    sides_config_key: Optional[str] = None,
    output_format: str        = "PNG",
    skip_done: bool           = True,
    limit: Optional[int]      = None,
    listing_ids: Optional[list] = None,
):
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load art list ─────────────────────────────────────────────────────────
    if os.path.exists(extract_log_csv):
        log_df = load_extract_log(extract_log_csv)
    else:
        log_df = _scan_arts_dir(arts_dir)

    if log_df.empty:
        done("Không có art nào để ghép.")
        return pd.DataFrame()

    work = build_work_list(log_df, skip_done=skip_done, listing_ids=listing_ids)

    if listing_ids:
        skip_done = False   # chỉ định ID cụ thể → luôn redo

    if limit:
        work = work[:limit]
        info(f"[LIMIT] --limit {limit}: xử lý {len(work)} listings đầu tiên")

    if not work:
        done("Không có listing nào để ghép.")
        return pd.DataFrame()

    # ── Scan mockups ──────────────────────────────────────────────────────────
    single_mockups = scan_mockups(mockup_folder, label="single")
    sides_mockups  = scan_mockups(sides_folder,  label="sides") if sides_folder else []

    if not single_mockups and not sides_mockups:
        err("Không tìm thấy mockup nào — kiểm tra --mockup-folder / --sides-folder")
        return pd.DataFrame()

    # ── Resolve config keys ───────────────────────────────────────────────────
    eff_single = config_key       or (list(SINGLE_CONFIGS.keys())[0] if SINGLE_CONFIGS else None)
    eff_sides  = sides_config_key or (list(SIDES_CONFIGS.keys())[0]  if SIDES_CONFIGS  else None)

    if not eff_single and mode in ("single", "auto"):
        err("Không có single config trong configs.json")
        return pd.DataFrame()

    info(f"[CONFIG] single={eff_single}  sides={eff_sides}  mode={mode}")
    info(f"[OUTPUT] {output_dir}/")

    print(f"\n{'═'*60}")
    print(f"  Tổng listings : {len(work)}")
    print(f"  Single mockup : {len(single_mockups)}")
    print(f"  Sides mockup  : {len(sides_mockups)}")
    print(f"  Mode          : {mode}")
    print(f"{'═'*60}\n")

    # ── Main loop ─────────────────────────────────────────────────────────────
    all_log: list = []
    items_since_flush = 0
    setup_signal(all_log)
    total = len(work)

    for i, item in enumerate(work, 1):
        lid        = item["listing_id"]
        front_path = item["front_path"]
        back_path  = item["back_path"]
        has_back   = item["has_back"]

        print(f"\n{'─'*55}")
        info(f"[{i}/{total}] {lid}  has_back={has_back}")

        # Quyết định mode cho listing này
        listing_mode = mode
        if mode == "auto":
            listing_mode = "sides" if (has_back and sides_mockups and eff_sides) else "single"

        if listing_mode == "sides":
            if not has_back:
                warn(f"  Không có back art — fallback sang single")
                listing_mode = "single"
            elif not sides_mockups:
                warn(f"  Không có sides mockup — fallback sang single")
                listing_mode = "single"
            elif not eff_sides:
                warn(f"  Không có sides config — fallback sang single")
                listing_mode = "single"

        if listing_mode == "sides":
            entries = _compose_sides(
                lid, front_path, back_path,
                sides_mockups, eff_sides,
                output_dir, output_format, skip_done,
            )
        else:
            entries = _compose_single(
                lid, front_path,
                single_mockups, eff_single,
                output_dir, output_format, skip_done,
            )

        all_log.extend(entries)
        items_since_flush += 1

        if items_since_flush >= CHECKPOINT_EVERY:
            flush_log(all_log)
            items_since_flush = 0

    flush_log(all_log)

    # ── Summary ───────────────────────────────────────────────────────────────
    df = pd.DataFrame(all_log)
    done_cnt   = (df["status"] == "done").sum()    if not df.empty else 0
    skip_cnt   = (df["status"] == "skipped").sum() if not df.empty else 0
    failed_cnt = (df["status"] == "failed").sum()  if not df.empty else 0
    avg = (
        df[df["status"] == "done"]["elapsed"].astype(float).mean()
        if done_cnt else 0
    )

    print(f"\n{'═'*60}")
    done(f"Tổng output : {len(all_log)}  ✓ {done_cnt}  ⟳ {skip_cnt}  ✗ {failed_cnt}  (~{avg:.1f}s/ảnh)")
    done(f"Output      : {output_dir}/")
    done(f"Log         : {COMPOSE_LOG}")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch ghép art lên mockup người mẫu (bước cuối pipeline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Pipeline: annotate_crop.py → remove_background.py → compose_mockup.py",
            "",
            "Ví dụ:",
            "  python mockup-generator/compose_mockup.py --mockup-folder mockup_photos/",
            "  python mockup-generator/compose_mockup.py \\",
            "    --mockup-folder mockup_photos/ --sides-folder sides_photos/ --mode auto",
            "  python mockup-generator/compose_mockup.py \\",
            "    --mockup-folder mockup_photos/ --config pose_B --limit 5",
            "  python mockup-generator/compose_mockup.py \\",
            "    --mockup-folder mockup_photos/ --listing-ids 12345 67890 --redo",
            "  python mockup-generator/compose_mockup.py --list-configs",
        ]),
    )
    parser.add_argument("--extract-log",     default=EXTRACT_LOG,
                        help=f"CSV log từ remove_background.py (default: {EXTRACT_LOG})")
    parser.add_argument("--arts-dir",        default=str(ARTS_DIR),
                        help=f"Folder art đã xóa phông — dùng khi không có extract-log "
                             f"(default: {ARTS_DIR})")
    parser.add_argument("--mockup-folder",   default=str(MOCKUP_FOLDER),
                        help=f"Folder mockup người mẫu (default: {MOCKUP_FOLDER})")
    parser.add_argument("--sides-folder",    default=None,
                        help="Folder mockup 2 mặt (dùng với --mode sides/auto)")
    parser.add_argument("--output",          default=str(OUTPUT_DIR),
                        help=f"Folder xuất kết quả (default: {OUTPUT_DIR})")
    parser.add_argument("--mode",            default="single",
                        choices=["single", "sides", "auto"],
                        help="single=chỉ front | sides=front+back | auto=tự detect (default: single)")
    parser.add_argument("--config",          default=None,
                        help="Single config key (default: key đầu tiên trong configs.json)")
    parser.add_argument("--sides-config",    default=None,
                        help="Sides config key  (default: key đầu tiên trong configs.json)")
    parser.add_argument("--format",          default="PNG", choices=["PNG", "JPG", "JPEG"])
    parser.add_argument("--redo",            action="store_true",
                        help="Redo listing đã có output")
    parser.add_argument("--limit",           type=int, default=None,
                        help="Giới hạn số listing xử lý")
    parser.add_argument("--listing-ids",     nargs="+", metavar="ID",
                        help="Chỉ xử lý listing ID cụ thể (tự động redo)")
    parser.add_argument("--list-configs",    action="store_true",
                        help="Liệt kê tất cả configs rồi thoát")

    args = parser.parse_args()

    if args.list_configs:
        list_configs()
        sys.exit(0)

    compose_all(
        mockup_folder    = Path(args.mockup_folder),
        extract_log_csv  = args.extract_log,
        arts_dir         = Path(args.arts_dir),
        sides_folder     = Path(args.sides_folder) if args.sides_folder else None,
        output_dir       = Path(args.output),
        mode             = args.mode,
        config_key       = args.config,
        sides_config_key = args.sides_config,
        output_format    = args.format,
        skip_done        = not args.redo,
        limit            = args.limit,
        listing_ids      = args.listing_ids,
    )
