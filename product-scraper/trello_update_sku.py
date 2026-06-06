"""
trello_update_sku.py
────────────────────
Update card name (SKU) + description cho tất cả cards trên board.

SKU dựa vào ngày tạo card (extract từ createCard action hoặc card ID):
  NTD{ddmmyy}{letter}{counter}  VD: NTD050626A01

Update toàn bộ, kể cả card đã có SKU đúng định dạng.
"""

import os
import sys
import re
import time
import signal
import requests
import pandas as pd
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
TRELLO_API_KEY  = os.getenv("TRELLO_API_KEY", "")
TRELLO_TOKEN    = os.getenv("TRELLO_TOKEN", "")
TRELLO_BOARD_ID = ""

INPUT_CSV    = "heyetsy_image_urls.csv"
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
def card_id_to_date_vn(card_id: str) -> datetime:
    """Extract ngày tạo từ card ID (MongoDB ObjectID), convert sang UTC+7."""
    timestamp = int(card_id[:8], 16)
    dt_utc    = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone(VN_OFFSET))

def get_create_date_from_action(card_id: str) -> Optional[datetime]:
    """
    Gọi Actions API lấy ngày createCard chính xác.
    Trả về datetime UTC+7 hoặc None nếu không có.
    """
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

def get_card_date_str(card_id: str) -> str:
    """
    Lấy date string dạng ddmmyy (UTC+7) để dùng trong SKU.
    Ưu tiên Actions API, fallback về extract từ card ID.
    """
    dt = get_create_date_from_action(card_id)
    if dt is None:
        dt = card_id_to_date_vn(card_id)
    return dt.strftime("%d%m%y")


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
            c["list_id"]   = lst["id"]
        all_cards.extend(cards)
    return all_cards


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    df = None
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
            info(f"Đọc file với encoding: {enc}")
            break
        except UnicodeDecodeError:
            if enc == "latin-1":
                raise
            continue
    for col in ["shop_name", "listing_id", "title"]:
        if col not in df.columns:
            raise ValueError(f"CSV thiếu cột: '{col}'")
    info(f"Loaded {len(df)} rows từ {csv_path}")
    return df

def build_meta_map(df: pd.DataFrame) -> Dict[str, Dict]:
    meta = {}
    for _, row in df.iterrows():
        title = (row.get("title") or "").strip()
        if title:
            meta[title] = {
                "title":    title,
                "tags":     (row.get("tags") or "").strip(),
                "etsy_url": (row.get("etsy_url") or row.get("url") or "").strip(),
            }
    return meta


# ── SKU & Description helpers ─────────────────────────────────────────────────
def make_sku(counter: int, date_str: str) -> str:
    return f"{SKU_PREFIX}{date_str}{SKU_LETTER}{str(counter).zfill(2)}"

def extract_title_from_desc(desc: str) -> str:
    m = re.search(r"\*\*Title:\*\*\s*(.+)", desc or "")
    return m.group(1).strip() if m else ""

def build_expected_desc(meta: Dict) -> str:
    parts = []
    if meta.get("title"):
        parts.append(f"**Title:** {meta['title']}")
    if meta.get("tags"):
        parts.append(f"**Tags:** {meta['tags']}")
    if meta.get("etsy_url"):
        parts.append(f"**Etsy URL:** {meta['etsy_url']}")
    return "\n\n".join(parts)

def desc_is_correct(current: str, expected: str) -> bool:
    normalize = lambda s: re.sub(r"\s+", " ", s.strip())
    return normalize(current) == normalize(expected)


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng sau card hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Startup flow (B1 → B3) ────────────────────────────────────────────────────
def startup_check(
    board_id:    str,
    total_cards: int,
    list_stats:  List[Dict],
    df:          pd.DataFrame,
) -> int:
    total_listings = len(df)
    shops          = df["shop_name"].unique().tolist()

    sep()
    info(f"Board hiện có {len(list_stats)} List(s) active.")

    if total_cards == 0:
        info("Board rỗng — không có card nào để update.")
        sys.exit(0)

    # B1: Breakdown + gợi ý resume từng list
    trello_card_count = {s["name"]: s["card_count"] for s in list_stats}

    info(f"Tìm thấy {total_cards} card (tổng {total_listings} listings), trong đó:")
    running            = 0
    resume_suggestions = []

    for shop_name in shops:
        shop_total      = len(df[df["shop_name"] == shop_name])
        cards_in_trello = trello_card_count.get(shop_name, 0)
        print(f"  * {cards_in_trello} card trong List {shop_name} (tổng {shop_total} listings)")
        if cards_in_trello > 0:
            resume_row = running + cards_in_trello + 1   # 1-based, dòng tiếp theo
            resume_suggestions.append((shop_name, cards_in_trello, resume_row))
        running += shop_total

    # Tính suggested: dòng tiếp theo sau shop/card cuối đã upload
    suggested = 0
    running   = 0
    for shop_name in shops:
        shop_total      = len(df[df["shop_name"] == shop_name])
        cards_in_trello = trello_card_count.get(shop_name, 0)
        if cards_in_trello == 0:
            break
        if cards_in_trello >= shop_total:
            running   += shop_total
            suggested  = running
        else:
            suggested = running + cards_in_trello
            break

    print()
    info("Gợi ý resume từng list:")
    for shop_name, cards_done, global_row in resume_suggestions:
        print(f"  * resume list {shop_name} tại card {cards_done} [nhập {global_row}]")

    info(f"Đề xuất tiếp tục từ card thứ {suggested + 1}/{total_cards}")
    sep()

    # B2: Resume hay từ 1
    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục từ card {suggested + 1}/{total_cards}? (Y/n): "
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
            f"Nhập số thứ tự card bắt đầu [mặc định {suggested + 1}]: "
        ).strip()
        start_idx = int(custom) - 1 if custom.isdigit() else suggested

    info(f"Sẽ bắt đầu check/update từ card thứ {start_idx + 1}/{total_cards}")
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
def run(
    csv_path: str = INPUT_CSV,
    board_id: str = TRELLO_BOARD_ID,
) -> None:
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        sys.exit(1)

    setup_signal()

    df       = load_csv(csv_path)
    meta_map = build_meta_map(df)

    # B1: đếm cards
    info("Đang đếm cards trên board...")
    list_stats  = count_cards_per_list(board_id)
    total_cards = sum(s["card_count"] for s in list_stats)

    # B1 → B3
    start_idx = startup_check(board_id, total_cards, list_stats, df)

    # Fetch toàn bộ cards
    info("Fetching toàn bộ cards trên board...")
    all_cards = fetch_all_cards(board_id)
    info(f"Loaded {len(all_cards)} cards.")
    sep()

    cards_to_process = all_cards[start_idx:]

    # date_counter_map: {date_str → counter hiện tại}
    # Mỗi ngày có counter riêng, tiếp tục đúng chỗ khi quay lại ngày cũ
    date_counter_map: Dict[str, int] = {}

    # Nếu resume từ giữa: pre-scan toàn bộ card trước start_idx
    # để biết counter hiện tại của từng ngày
    if start_idx > 0:
        info(f"Pre-scan {start_idx} cards trước start_idx để tính counter...")
        for c in all_cards[:start_idx]:
            d = get_card_date_str(c["id"])
            date_counter_map[d] = date_counter_map.get(d, 0) + 1
        for d, cnt in sorted(date_counter_map.items()):
            info(f"  Ngày {d}: đã dùng {cnt} counter (tiếp theo = {cnt + 1})")

    updated_name = 0
    updated_desc = 0
    already_ok   = 0
    no_meta      = 0

    for i, card in enumerate(cards_to_process, start_idx + 1):
        if _interrupted:
            break

        card_id      = card["id"]
        current_name = card["name"]
        current_desc = card.get("desc", "")
        list_name    = card.get("list_name", "")

        # ── Lấy ngày tạo card (UTC+7) ─────────────────────────────────────────
        date_str = get_card_date_str(card_id)

        # ── Lấy counter tiếp theo của ngày này ───────────────────────────────
        sku_counter = date_counter_map.get(date_str, 0) + 1

        # ── Lookup metadata ───────────────────────────────────────────────────
        title_in_desc = extract_title_from_desc(current_desc)
        meta          = meta_map.get(title_in_desc) or meta_map.get(current_name)

        if not meta:
            warn(f"  [{i}/{total_cards}] '{current_name[:50]}' — không tìm thấy metadata, skip")
            no_meta += 1
            # Vẫn tăng counter ngày này để giữ đúng thứ tự SKU
            date_counter_map[date_str] = sku_counter
            time.sleep(DELAY_UPDATE)
            continue

        # ── Tính giá trị expected ─────────────────────────────────────────────
        expected_name = make_sku(sku_counter, date_str)
        expected_desc = build_expected_desc(meta)

        name_ok = current_name == expected_name
        desc_ok = desc_is_correct(current_desc, expected_desc)

        # Tăng counter của ngày này trước khi xử lý tiếp
        date_counter_map[date_str] = sku_counter

        if name_ok and desc_ok:
            skip(f"  [{i}/{total_cards}] '{current_name}' ({date_str}) — OK, skip")
            already_ok += 1
            time.sleep(DELAY_UPDATE)
            continue

        # ── Update ────────────────────────────────────────────────────────────
        payload   = {}
        log_parts = []

        if not name_ok:
            payload["name"] = expected_name
            log_parts.append(f"name: '{current_name}' → '{expected_name}' ({date_str})")

        if not desc_ok:
            payload["desc"] = expected_desc
            log_parts.append("desc: updated")

        try:
            _put(f"/cards/{card_id}", payload)
            upd(f"  [{i}/{total_cards}] {' | '.join(log_parts)}  (list: {list_name})")
            if not name_ok:
                updated_name += 1
            if not desc_ok:
                updated_desc += 1
        except requests.HTTPError as e:
            err(f"  [{i}/{total_cards}] Update thất bại: {e}")

        time.sleep(DELAY_UPDATE)

    # ── Summary ───────────────────────────────────────────────────────────────
    sep()
    done("Update hoàn tất!")
    info(f"  Đã update name (SKU) : {updated_name}")
    info(f"  Đã update description: {updated_desc}")
    info(f"  Đã đúng (skip)       : {already_ok}")
    info(f"  Không có metadata    : {no_meta}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()