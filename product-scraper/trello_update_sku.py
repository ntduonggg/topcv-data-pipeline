"""
trello_update_sku.py
────────────────────
Chỉ update SKU (card name) — đưa tất cả SKU về đúng thứ tự
theo thời gian tạo card (createCard date, UTC+7), tính trên TOÀN BOARD.

Format card name: [NTD{ddmmyy}A{counter}] {Title}
  - counter reset về 01 mỗi khi sang ngày mới
  - thứ tự tính theo createCard date, KHÔNG theo thứ tự list/card hiện tại

Title được giữ nguyên từ phần sau "]" trong tên card hiện tại.
Không động vào description.
"""

import os
import sys
import re
import time
import signal
import requests
from datetime import datetime, timezone, timedelta
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
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,      'CKPT')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR,     'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,      'DONE')}  {msg}")
def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,      'SKIP')}  {msg}")
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")
def upd(msg):   print(f"{ts()} {C.tag(C.CKPT,      'UPD')}   {msg}")
def sep():      print("─" * 60)


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = os.environ.get("TRELLO_API_KEY", "")
TRELLO_TOKEN    = os.environ.get("TRELLO_TOKEN", "")
TRELLO_BOARD_ID = os.environ.get("TRELLO_BOARD_ID", "")

DELAY_UPDATE = 0.3

SKU_PREFIX = "NTD"
SKU_LETTER = "A"
VN_OFFSET  = timedelta(hours=7)   # UTC+7

TRELLO_BASE = "https://api.trello.com/1"


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


# ── Date helpers ──────────────────────────────────────────────────────────────
def card_id_to_datetime_vn(card_id: str) -> datetime:
    """Extract datetime tạo từ card ID (MongoDB ObjectID), convert sang UTC+7."""
    timestamp = int(card_id[:8], 16)
    dt_utc    = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone(VN_OFFSET))

def get_create_datetime_from_action(card_id: str) -> Optional[datetime]:
    """Gọi Actions API lấy datetime createCard chính xác (UTC+7)."""
    try:
        actions = _get(
            f"/cards/{card_id}/actions",
            {"filter": "createCard", "fields": "date", "limit": "1"}
        )
        if actions:
            date_str = actions[0]["date"]   # VD: "2026-06-05T02:59:13.180Z"
            dt_utc   = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            dt_utc   = dt_utc.replace(tzinfo=timezone.utc)
            return dt_utc.astimezone(timezone(VN_OFFSET))
    except Exception:
        pass
    return None

def get_card_created_at(card_id: str) -> datetime:
    """
    Lấy datetime tạo card (UTC+7).
    Ưu tiên Actions API, fallback extract từ card ID.
    """
    dt = get_create_datetime_from_action(card_id)
    if dt is None:
        dt = card_id_to_datetime_vn(card_id)
    return dt


# ── Board helpers ─────────────────────────────────────────────────────────────
def fetch_board_lists(board_id: str) -> List[Dict]:
    return _get(f"/boards/{board_id}/lists", {"fields": "id,name"})

def fetch_all_cards(board_id: str) -> List[Dict]:
    """Fetch toàn bộ cards (id, name) trên board, không quan tâm list nào."""
    lists     = fetch_board_lists(board_id)
    all_cards = []
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id,name"})
        for c in cards:
            c["list_name"] = lst["name"]
        all_cards.extend(cards)
    return all_cards


# ── SKU & name helpers ────────────────────────────────────────────────────────
def make_sku(counter: int, date_str: str) -> str:
    return f"{SKU_PREFIX}{date_str}{SKU_LETTER}{str(counter).zfill(2)}"

def extract_title_from_name(name: str) -> str:
    """
    Tách phần title từ card name hiện tại.
    Hỗ trợ:
      - "[NTD120626A248] Stars Hollow Luke's Diner..."  → "Stars Hollow Luke's Diner..."
      - "Stars Hollow Luke's Diner..."                  → giữ nguyên (chưa có SKU)
    """
    m = re.match(r"^\[.*?\]\s*(.+)$", name.strip())
    if m:
        return m.group(1).strip()
    return name.strip()

def build_new_name(sku: str, title: str) -> str:
    return f"[{sku}] {title}"


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng sau card hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Startup flow (B1 → B3) ────────────────────────────────────────────────────
def startup_check(total_cards: int) -> bool:
    """
    B1: Hiển thị tổng số cards.
    B3: Xác nhận tiếp tục.
    Trả về True nếu user xác nhận tiếp tục.
    """
    sep()
    info(f"Board có {total_cards} cards.")
    info("Sẽ tính lại createCard date cho TẤT CẢ cards, sắp xếp theo thứ tự thời gian,")
    info("và renumber SKU [NTD{ddmmyy}A{counter}] — counter reset mỗi ngày mới.")
    sep()

    final = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục update SKU cho {total_cards} cards? (Y/n): "
    ).strip().lower()

    if final == "n":
        info("Huỷ — thoát.")
        return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def run(board_id: str = TRELLO_BOARD_ID) -> None:
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        sys.exit(1)

    setup_signal()

    # ── Fetch toàn bộ cards ────────────────────────────────────────────────────
    info("Fetching toàn bộ cards trên board...")
    all_cards = fetch_all_cards(board_id)
    total_cards = len(all_cards)
    info(f"Loaded {total_cards} cards.")

    if total_cards == 0:
        info("Board rỗng — không có card nào để update.")
        return

    # B1 → B3
    if not startup_check(total_cards):
        return

    sep()

    # ── Lấy createCard datetime cho từng card ─────────────────────────────────
    info("Lấy createCard datetime cho từng card (có thể chậm)...")
    for i, card in enumerate(all_cards, 1):
        card["created_at"] = get_card_created_at(card["id"])
        if i % 50 == 0 or i == total_cards:
            info(f"  Đã lấy {i}/{total_cards} cards...")
        time.sleep(0.1)  # nhẹ tay với Actions API

    # ── Sort toàn bộ cards theo createCard datetime (tăng dần) ────────────────
    all_cards.sort(key=lambda c: c["created_at"])

    sep()
    info("Đã sort toàn bộ cards theo thứ tự thời gian tạo.")
    info(f"  Card đầu : {all_cards[0]['created_at']}  '{all_cards[0]['name'][:50]}'")
    info(f"  Card cuối: {all_cards[-1]['created_at']} '{all_cards[-1]['name'][:50]}'")
    sep()

    # ── Renumber SKU theo thứ tự đã sort ──────────────────────────────────────
    date_counter: Dict[str, int] = {}

    updated_count = 0
    already_ok    = 0

    for i, card in enumerate(all_cards, 1):
        if _interrupted:
            break

        card_id      = card["id"]
        current_name = card["name"]
        created_at   = card["created_at"]
        list_name    = card.get("list_name", "")

        date_str = created_at.strftime("%d%m%y")

        # Counter cho ngày này, tăng dần theo thứ tự đã sort
        date_counter[date_str] = date_counter.get(date_str, 0) + 1
        counter = date_counter[date_str]

        expected_sku  = make_sku(counter, date_str)
        title         = extract_title_from_name(current_name)
        expected_name = build_new_name(expected_sku, title)

        if current_name == expected_name:
            skip(f"  [{i}/{total_cards}] '{expected_sku}' — OK, skip")
            already_ok += 1
            continue

        try:
            _put(f"/cards/{card_id}", {"name": expected_name})
            upd(f"  [{i}/{total_cards}] '{current_name[:40]}' → '[{expected_sku}] {title[:40]}'  "
                f"(created: {created_at.strftime('%d/%m/%Y %H:%M')}, list: {list_name})")
            updated_count += 1
        except requests.HTTPError as e:
            err(f"  [{i}/{total_cards}] Update thất bại: {e}")

        time.sleep(DELAY_UPDATE)

    # ── Summary ───────────────────────────────────────────────────────────────
    sep()
    done("Update SKU hoàn tất!")
    info(f"  Tổng cards     : {total_cards}")
    info(f"  Đã update      : {updated_count}")
    info(f"  Đã đúng (skip) : {already_ok}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()