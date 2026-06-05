"""
update_card_info.py
────────────────────
Check và update card name (SKU) + description (Title, Tags, Etsy URL)
cho tất cả cards trên board theo dữ liệu từ heyetsy_image_urls.csv.

Dạng chuẩn:
  Card name : NTD{ddmmyy}{letter}{counter}  VD: NTD050626A01
  Description:
    **Title:** <title>
    **Tags:** <tags>
    **Etsy URL:** <url>

Flow:
  B1: Đếm cards theo list, hiển thị breakdown
  B2: Chọn dòng bắt đầu (resume hoặc từ đầu)
  B3: Xác nhận lần cuối → tiến hành update
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
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,      'CKPT')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR,     'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,      'DONE')}  {msg}")
def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,      'SKIP')}  {msg}")
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")
def upd(msg):   print(f"{ts()} {C.tag(C.CKPT,      'UPD')}   {msg}")
def sep():      print("─" * 60)


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = ""
TRELLO_TOKEN    = ""
TRELLO_BOARD_ID = ""

INPUT_CSV    = "heyetsy_image_urls.csv"
DELAY_UPDATE = 0.3   # giây giữa mỗi API call update

# SKU prefix & letter (phải khớp với trello_uploader.py)
SKU_PREFIX = "NTD"
SKU_LETTER = "A"

# Regex nhận dạng SKU hợp lệ: NTD{6 chữ số}{letter}{1+ chữ số}
SKU_REGEX = re.compile(r"^[A-Z]+\d{6}[A-Z]\d+$")

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
    """
    Fetch toàn bộ cards trên board (tất cả lists) kèm name + desc.
    Trả về list theo thứ tự list → card.
    """
    lists = fetch_board_lists(board_id)
    all_cards = []
    for lst in lists:
        cards = _get(
            f"/lists/{lst['id']}/cards",
            {"fields": "id,name,desc"}
        )
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
    """Tạo {title → {title, tags, etsy_url}} để lookup nhanh."""
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

def is_valid_sku(name: str) -> bool:
    return bool(SKU_REGEX.match(name))

def get_sku_counter(board_id: str) -> int:
    """Lấy counter SKU tiếp theo dựa vào card cuối cùng trên board."""
    today = datetime.now().strftime("%d%m%y")
    try:
        lists = fetch_board_lists(board_id)
        for lst in reversed(lists):
            cards = _get(f"/lists/{lst['id']}/cards", {"fields": "name"})
            if not cards:
                continue
            m = re.match(r"[A-Z]+(\d{6})[A-Z](\d+)$", cards[-1]["name"])
            if not m:
                break
            return int(m.group(2)) + 1 if m.group(1) == today else 1
    except Exception:
        pass
    return 1

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

def desc_is_correct(current_desc: str, expected_desc: str) -> bool:
    """So sánh description hiện tại với expected (normalize whitespace)."""
    normalize = lambda s: re.sub(r"\s+", " ", s.strip())
    return normalize(current_desc) == normalize(expected_desc)


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng sau card hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Startup flow (B1 → B3) ────────────────────────────────────────────────────
def startup_check(board_id: str, total_cards: int, list_stats: List[Dict], df: pd.DataFrame) -> int:
    """
    B1: Hiển thị breakdown cards theo list.
    B2: Chọn dòng bắt đầu (resume hoặc từ 1).
    B3: Xác nhận lần cuối.
    Trả về start_idx (0-based, tính theo thứ tự card trên board).
    """
    total_listings = len(df)
    shops          = df["shop_name"].unique().tolist()

    sep()
    info(f"Board hiện có {len(list_stats)} List(s) active.")

    if total_cards == 0:
        info("Board rỗng — không có card nào để update.")
        sep()
        sys.exit(0)

    info(f"Tìm thấy {total_cards} card (tổng {total_listings} listings), trong đó:")
    for s in list_stats:
        shop_total = len(df[df["shop_name"] == s["name"]]) if s["name"] in shops else "?"
        print(f"  * {s['card_count']} card trong List {s['name']} (tổng {shop_total} listings)")

    sep()

    # B2: Resume hay từ đầu
    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục từ dòng {total_cards}/{total_cards}? (Y/n): "
    ).strip().lower()

    if ans == "n":
        custom = input(
            f"{ts()} {C.tag(C.INFO, 'INFO')}  "
            f"Nhập số thứ tự card bắt đầu (1-based) [mặc định 1]: "
        ).strip()
        start_idx = int(custom) - 1 if custom.isdigit() else 0
    else:
        # Resume: gợi ý mặc định là card cuối + 1
        custom = input(
            f"{ts()} {C.tag(C.INFO, 'INFO')}  "
            f"Nhập số thứ tự card bắt đầu [mặc định {total_cards + 1}]: "
        ).strip()
        start_idx = int(custom) - 1 if custom.isdigit() else total_cards

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

    # ── B1: Đếm cards ─────────────────────────────────────────────────────────
    info("Đang đếm cards trên board...")
    list_stats  = count_cards_per_list(board_id)
    total_cards = sum(s["card_count"] for s in list_stats)

    # ── B1 → B3: Startup check ────────────────────────────────────────────────
    start_idx = startup_check(board_id, total_cards, list_stats, df)

    # ── Fetch toàn bộ cards ────────────────────────────────────────────────────
    info("Fetching toàn bộ cards trên board...")
    all_cards = fetch_all_cards(board_id)
    info(f"Loaded {len(all_cards)} cards.")
    sep()

    # SKU counter bắt đầu = start_idx + 1 (vì counter 1-based)
    today       = datetime.now().strftime("%d%m%y")
    sku_counter = start_idx + 1

    updated_name = 0
    updated_desc = 0
    already_ok   = 0
    no_meta      = 0

    cards_to_process = all_cards[start_idx:]

    for i, card in enumerate(cards_to_process, start_idx + 1):
        if _interrupted:
            break

        card_id      = card["id"]
        current_name = card["name"]
        current_desc = card.get("desc", "")
        list_name    = card.get("list_name", "")

        # Lookup metadata theo title trong description
        title_in_desc = extract_title_from_desc(current_desc)
        meta = meta_map.get(title_in_desc)

        # Fallback: nếu card name là title (card cũ chưa có SKU)
        if not meta:
            meta = meta_map.get(current_name)

        if not meta:
            warn(f"  [{i}/{total_cards}] '{current_name[:50]}' — không tìm thấy metadata, skip")
            no_meta += 1
            sku_counter += 1
            time.sleep(DELAY_UPDATE)
            continue

        # Tính giá trị expected
        expected_name = make_sku(sku_counter, today)
        expected_desc = build_expected_desc(meta)

        name_ok = is_valid_sku(current_name) and current_name == expected_name
        desc_ok = desc_is_correct(current_desc, expected_desc)

        if name_ok and desc_ok:
            skip(f"  [{i}/{total_cards}] '{current_name}' — OK, skip")
            already_ok  += 1
            sku_counter += 1
            time.sleep(DELAY_UPDATE)
            continue

        # Cần update
        payload = {}
        log_parts = []

        if not name_ok:
            payload["name"] = expected_name
            log_parts.append(f"name: '{current_name}' → '{expected_name}'")

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

        sku_counter += 1
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