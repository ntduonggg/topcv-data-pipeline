"""
trello_format_cards.py
──────────────────────
Update card name + description trực tiếp từ Trello board.

Dạng chuẩn sau update:
  Card name : [NTD050626A32] Personalized First Disney Cruise Magnet...
  Description: disney magnet, disney cruise magnet, ...  (chỉ tags, không prefix "Tags:")

- Nếu card đã có description → parse + normalize (; → ,) + update nếu sai format
- Nếu card KHÔNG có description → lookup tags từ CSV theo title trong card name
"""

import os
import sys
import re
import time
import signal
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO      = "\033[94m"
    WARN      = "\033[93m"
    CKPT      = "\033[92m"
    ERROR     = "\033[91m"
    INTERRUPT = "\033[95m"
    TIME      = "\033[96m"
    DONE      = "\033[92m"
    SKIP      = "\033[90m"
    RESET     = "\033[0m"

    @staticmethod
    def tag(color: str, label: str) -> str:
        return f"{color}[{label}]{C.RESET}"

def ts() -> str:
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,      'INFO')}  {msg}")
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,      'WARN')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR,     'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,      'DONE')}  {msg}")
def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,      'SKIP')}  {msg}")
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")
def upd(msg):   print(f"{ts()} {C.tag(C.CKPT,      'UPD')}   {msg}")
def sep():      print("─" * 60)


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = os.abort("TRELLO_API_KEY not set in environment") if "TRELLO_API_KEY" not in os.environ else os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN    = os.abort("TRELLO_TOKEN not set in environment") if "TRELLO_TOKEN" not in os.environ else os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.abort("TRELLO_BOARD_ID not set in environment") if "TRELLO_BOARD_ID" not in os.environ else os.getenv("TRELLO_BOARD_ID")

DELAY_UPDATE = 0.3   # giây giữa mỗi API call update
INPUT_CSV    = "heyetsy_image_urls.csv"   # dùng khi card không có description

TRELLO_BASE = "https://api.trello.com/1"

# Regex nhận dạng SKU hợp lệ: NTD{6 số}{letter}{1+ số}
SKU_REGEX = re.compile(r"^[A-Z]+\d{6}[A-Z]\d+$")


# ── Trello API helpers ────────────────────────────────────────────────────────
def _auth() -> Dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}

def _get(endpoint: str, params: Dict = {}) -> dict | list:
    resp = requests.get(f"{TRELLO_BASE}{endpoint}", params={**_auth(), **params}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _put(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.put(f"{TRELLO_BASE}{endpoint}", params={**_auth(), **payload}, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Board helpers ─────────────────────────────────────────────────────────────
def fetch_board_lists(board_id: str) -> List[Dict]:
    return _get(f"/boards/{board_id}/lists", {"fields": "id,name"})

def count_cards_per_list(board_id: str) -> List[Dict]:
    lists = fetch_board_lists(board_id)
    result = []
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id"})
        result.append({"name": lst["name"], "id": lst["id"], "card_count": len(cards)})
    return result

def fetch_all_cards(board_id: str) -> List[Dict]:
    """Fetch toàn bộ cards (id, name, desc) theo thứ tự list → card."""
    lists     = fetch_board_lists(board_id)
    all_cards = []
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id,name,desc"})
        for c in cards:
            c["list_name"] = lst["name"]
        all_cards.extend(cards)
    return all_cards


# ── CSV helpers ──────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
            info(f"Loaded CSV: {len(df)} rows (encoding={enc})")
            return df
        except UnicodeDecodeError:
            if enc == "latin-1":
                raise
            continue
    raise RuntimeError(f"Không đọc được {csv_path}")

def build_tags_map(df: pd.DataFrame) -> Dict[str, str]:
    """
    Tạo {title → tags_normalized} từ CSV.
    Tags normalize: tách theo ; hoặc , rồi join bằng ", ".
    """
    result = {}
    for _, row in df.iterrows():
        title = (row.get("title") or "").strip()
        tags  = (row.get("tags")  or "").strip()
        if title and tags:
            normalized = ", ".join(t.strip() for t in re.split(r"[;,]", tags) if t.strip())
            result[title] = normalized
    return result


# ── Parse helpers ─────────────────────────────────────────────────────────────
def parse_sku_from_name(name: str) -> Optional[str]:
    """
    Extract SKU từ card name.
    Hỗ trợ 2 dạng:
      - SKU thuần: "NTD050626A32"
      - SKU + title: "[NTD050626A32] Personalized First Disney..."
    """
    # Dạng [SKU] title
    m = re.match(r"^\[([A-Z]+\d{6}[A-Z]\d+)\]", name)
    if m:
        return m.group(1)
    # Dạng SKU thuần
    if SKU_REGEX.match(name.strip()):
        return name.strip()
    return None

def parse_title_from_desc(desc: str) -> Optional[str]:
    """Extract title từ **Title:** trong description."""
    m = re.search(r"\*\*Title:\*\*\s*(.+?)(?:\n|$)", desc or "")
    return m.group(1).strip() if m else None

def parse_tags_from_desc(desc: str) -> Optional[str]:
    """
    Extract tags từ description, normalize separator thành ', '.
    Hỗ trợ:
      - **Tags:** tag1; tag2; tag3  →  tag1, tag2, tag3
      - **Tags:** tag1, tag2, tag3  → giữ nguyên
    """
    m = re.search(r"\*\*Tags:\*\*\s*(.+?)(?:\n\n|\Z)", desc or "", re.DOTALL)
    if m:
        raw = m.group(1).strip()
        # Normalize: tách theo ; hoặc , rồi join lại bằng ", "
        tags = [t.strip() for t in re.split(r"[;,]", raw) if t.strip()]
        return ", ".join(tags)
    return None

def build_new_name(sku: str, title: str) -> str:
    """[SKU] Title"""
    return f"[{sku}] {title}"

def build_new_desc(tags: Optional[str]) -> str:
    """Chỉ còn tags, không có prefix 'Tags:', không có Title, không có Etsy URL."""
    return tags.strip() if tags else ""

def name_is_correct(name: str) -> bool:
    """Card name đúng dạng [SKU] Title."""
    return bool(re.match(r"^\[[A-Z]+\d{6}[A-Z]\d+\] .+", name))

def desc_is_correct(desc: str) -> bool:
    """
    Description đúng nếu:
    - Không chứa **Title:**
    - Không chứa **Tags:**
    - Không chứa **Etsy URL:**
    """
    return (
        "**Title:**" not in (desc or "")
        and "**Tags:**" not in (desc or "")
        and "**Etsy URL:**" not in (desc or "")
    )


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng sau card hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Startup flow (B1 → B3) ────────────────────────────────────────────────────
def startup_check(board_id: str, list_stats: List[Dict]) -> int:
    total_cards = sum(s["card_count"] for s in list_stats)

    sep()
    info(f"Board hiện có {len(list_stats)} List(s).")

    if total_cards == 0:
        info("Board rỗng — không có card nào để update.")
        sys.exit(0)

    # B1: Breakdown
    info(f"Tìm thấy {total_cards} card, trong đó:")
    running            = 0
    resume_suggestions = []
    for s in list_stats:
        print(f"  * {s['card_count']} card trong List {s['name']}")
        if s["card_count"] > 0:
            resume_row = running + s["card_count"] + 1
            resume_suggestions.append((s["name"], s["card_count"], resume_row))
        running += s["card_count"]

    # Gợi ý resume từng list
    print()
    info("Gợi ý resume từng list:")
    for shop_name, cards_done, global_row in resume_suggestions:
        print(f"  * resume list {shop_name} tại card {cards_done} [nhập {global_row}]")

    sep()

    # B2: Chọn dòng bắt đầu
    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục từ card 1/{total_cards}? (Y/n): "
    ).strip().lower()

    if ans == "n":
        custom = input(
            f"{ts()} {C.tag(C.INFO, 'INFO')}  "
            f"Nhập số thứ tự card bắt đầu (1-based) [mặc định 1]: "
        ).strip()
        start_idx = int(custom) - 1 if custom.isdigit() else 0
    else:
        custom = input(
            f"{ts()} {C.tag(C.INFO, 'INFO')}  "
            f"Nhập số thứ tự card bắt đầu [mặc định 1]: "
        ).strip()
        start_idx = int(custom) - 1 if custom.isdigit() else 0

    info(f"Sẽ bắt đầu từ card thứ {start_idx + 1}/{total_cards}")
    sep()

    # B3: Xác nhận
    final = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục? (Y/n): "
    ).strip().lower()

    if final == "n":
        info("Huỷ — thoát.")
        sys.exit(0)

    return start_idx


# ── Main ──────────────────────────────────────────────────────────────────────
def run(board_id: str = TRELLO_BOARD_ID, csv_path: str = INPUT_CSV) -> None:
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        sys.exit(1)

    setup_signal()

    # Load CSV để fallback khi card không có description
    tags_map: Dict[str, str] = {}
    if os.path.exists(csv_path):
        try:
            df       = load_csv(csv_path)
            tags_map = build_tags_map(df)
            info(f"Tags map: {len(tags_map)} entries từ CSV")
        except Exception as e:
            warn(f"Không load được CSV: {e} — sẽ bỏ qua card không có description")
    else:
        warn(f"Không tìm thấy {csv_path} — sẽ bỏ qua card không có description")

    # B1: Đếm cards
    info("Đang đếm cards trên board...")
    list_stats  = count_cards_per_list(board_id)
    total_cards = sum(s["card_count"] for s in list_stats)

    # B1 → B3
    start_idx = startup_check(board_id, list_stats)

    # Fetch toàn bộ cards
    info("Fetching toàn bộ cards trên board...")
    all_cards = fetch_all_cards(board_id)
    info(f"Loaded {len(all_cards)} cards.")
    sep()

    cards_to_process = all_cards[start_idx:]
    updated_name  = 0
    updated_desc  = 0
    already_ok    = 0
    no_sku        = 0
    no_title      = 0
    added_from_csv = 0

    for i, card in enumerate(cards_to_process, start_idx + 1):
        if _interrupted:
            break

        card_id      = card["id"]
        current_name = card["name"]
        current_desc = card.get("desc", "") or ""
        list_name    = card.get("list_name", "")

        # ── Extract SKU từ card name ───────────────────────────────────────────
        sku = parse_sku_from_name(current_name)
        if not sku:
            warn(f"  [{i}/{total_cards}] '{current_name[:50]}' — không tìm thấy SKU, skip")
            no_sku += 1
            continue

        # ── Extract title từ description ──────────────────────────────────────
        title = parse_title_from_desc(current_desc)

        # ── Nếu card không có description → lookup từ CSV ─────────────────────
        if not current_desc.strip():
            # Card hoàn toàn rỗng description
            # Tìm title từ card name (nếu dạng [SKU] Title)
            m = re.match(r"^\[[A-Z]+\d{6}[A-Z]\d+\]\s*(.+)", current_name)
            title_from_name = m.group(1).strip() if m else None

            if title_from_name and title_from_name in tags_map:
                tags_csv = tags_map[title_from_name]
                info(f"  [{i}/{total_cards}] '{current_name[:50]}' — desc rỗng, thêm tags từ CSV")
                try:
                    _put(f"/cards/{card_id}", {"desc": tags_csv})
                    upd(f"  [{i}/{total_cards}] desc: added from CSV ({len(tags_csv)} chars)  (list: {list_name})")
                    added_from_csv += 1
                    updated_desc   += 1
                except requests.HTTPError as e:
                    err(f"  [{i}/{total_cards}] Add desc thất bại: {e}")
                time.sleep(DELAY_UPDATE)
                continue
            else:
                warn(f"  [{i}/{total_cards}] '{current_name[:50]}' — desc rỗng, không tìm thấy trong CSV, skip")
                no_title += 1
                continue

        if not title:
            warn(f"  [{i}/{total_cards}] '{current_name[:50]}' — không tìm thấy Title trong desc, skip")
            no_title += 1
            continue

        # ── Extract tags từ description ───────────────────────────────────────
        tags = parse_tags_from_desc(current_desc)

        # ── Tính expected ─────────────────────────────────────────────────────
        expected_name = build_new_name(sku, title)
        expected_desc = build_new_desc(tags)

        name_ok = name_is_correct(current_name) and current_name == expected_name
        desc_ok = desc_is_correct(current_desc) and current_desc == expected_desc

        if name_ok and desc_ok:
            skip(f"  [{i}/{total_cards}] '{current_name[:60]}' — OK, skip")
            already_ok += 1
            time.sleep(DELAY_UPDATE)
            continue

        # Log tóm tắt những gì cần update
        needs = []
        if not name_ok: needs.append("name")
        if not desc_ok: needs.append("desc")
        info(f"  [{i}/{total_cards}] '{current_name[:50]}' — cần update: {', '.join(needs)}")

        # ── Update name (nếu sai) ─────────────────────────────────────────────
        if not name_ok:
            try:
                _put(f"/cards/{card_id}", {"name": expected_name})
                upd(f"  [{i}/{total_cards}] name: '{current_name[:50]}' → '[{sku}] {title[:40]}'  (list: {list_name})")
                updated_name += 1
            except requests.HTTPError as e:
                err(f"  [{i}/{total_cards}] Update name thất bại: {e}")
            time.sleep(DELAY_UPDATE)

        # ── Update description (nếu sai) ──────────────────────────────────────
        if not desc_ok:
            try:
                _put(f"/cards/{card_id}", {"desc": expected_desc})
                upd(f"  [{i}/{total_cards}] desc: updated → tags only ({len(expected_desc)} chars)  (list: {list_name})")
                updated_desc += 1
            except requests.HTTPError as e:
                err(f"  [{i}/{total_cards}] Update desc thất bại: {e}")
            time.sleep(DELAY_UPDATE)

        # Skip delay nếu không có gì update
        if name_ok and not desc_ok or not name_ok and desc_ok:
            pass  # delay đã thực hiện trong block trên
        elif not name_ok and not desc_ok:
            pass  # 2 delay đã thực hiện
        else:
            time.sleep(DELAY_UPDATE)

    # ── Summary ───────────────────────────────────────────────────────────────
    sep()
    done("Format hoàn tất!")
    info(f"  Đã update name      : {updated_name}")
    info(f"  Đã update desc      : {updated_desc}")
    info(f"    Trong đó từ CSV   : {added_from_csv}")
    info(f"  Đã đúng (skip)      : {already_ok}")
    info(f"  Thiếu SKU (skip)    : {no_sku}")
    info(f"  Thiếu Title (skip)  : {no_title}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()