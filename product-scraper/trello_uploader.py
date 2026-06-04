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


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = ""
TRELLO_TOKEN    = ""
TRELLO_BOARD_ID = ""

INPUT_CSV = "heyetsy_image_urls.csv"

# Delay (tránh 429 Trello API)
DELAY_CARD        = 0.3   # giây giữa mỗi card
DELAY_ATTACHMENT  = 0.2   # giây giữa mỗi attachment
DELAY_LIST        = 1.0   # giây sau khi tạo list mới

TRELLO_BASE = "https://api.trello.com/1"

# Hướng dẫn lấy BOARD_ID:
# Mở board → thêm ".json" vào cuối URL → tìm field "id"
# VD: https://trello.com/b/AbCdEfGh/ten-board.json


# ── Trello API helpers ────────────────────────────────────────────────────────
def _auth() -> Dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}

def _get(endpoint: str, params: Dict = {}) -> dict | list:
    resp = requests.get(
        f"{TRELLO_BASE}{endpoint}",
        params={**_auth(), **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _post(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _put(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.put(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _delete(endpoint: str) -> bool:
    resp = requests.delete(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        timeout=15,
    )
    return resp.status_code == 200


# ── List helpers ──────────────────────────────────────────────────────────────
def fetch_existing_lists(board_id: str) -> Dict[str, str]:
    """Trả về {list_name → list_id}."""
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    return {lst["name"]: lst["id"] for lst in lists}

def create_list(board_id: str, name: str) -> str:
    result = _post("/lists", {"name": name, "idBoard": board_id, "pos": "bottom"})
    return result["id"]

def get_or_create_list(board_id: str, name: str, existing: Dict[str, str]) -> str:
    if name in existing:
        return existing[name]
    list_id = create_list(board_id, name)
    existing[name] = list_id
    done(f"  Tạo List '{name}' (id={list_id})")
    time.sleep(DELAY_LIST)
    return list_id


# ── Card helpers ──────────────────────────────────────────────────────────────
def fetch_existing_cards(list_id: str) -> Dict[str, str]:
    """Trả về {card_name → card_id}."""
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {card["name"]: card["id"] for card in cards}

def create_card(list_id: str, name: str, desc: str = "") -> str:
    result = _post("/cards", {
        "idList": list_id,
        "name":   name,
        "desc":   desc,
        "pos":    "bottom",
    })
    return result["id"]

def update_card(card_id: str, name: str, desc: str) -> bool:
    """Cập nhật title + description của card đã tồn tại."""
    try:
        _put(f"/cards/{card_id}", {"name": name, "desc": desc})
        return True
    except requests.HTTPError as e:
        warn(f"  Update card thất bại: {e}")
        return False


# ── Attachment helpers ────────────────────────────────────────────────────────
def fetch_card_attachments(card_id: str) -> List[Dict]:
    """Trả về list attachment hiện có của card."""
    try:
        return _get(f"/cards/{card_id}/attachments", {"fields": "id,url"})
    except Exception:
        return []

def delete_attachment(card_id: str, attachment_id: str) -> bool:
    return _delete(f"/cards/{card_id}/attachments/{attachment_id}")

def delete_all_attachments(card_id: str) -> int:
    """Xoá toàn bộ attachment của card. Trả về số lượng đã xoá."""
    attachments = fetch_card_attachments(card_id)
    deleted = 0
    for att in attachments:
        if delete_attachment(card_id, att["id"]):
            deleted += 1
        time.sleep(0.1)
    return deleted

def attach_url(card_id: str, url: str) -> bool:
    try:
        _post(f"/cards/{card_id}/attachments", {"url": url})
        return True
    except requests.HTTPError as e:
        warn(f"  Attachment thất bại ({url[:60]}...): {e}")
        return False


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    # sep=None + engine="python" → tự detect separator
    # encoding="cp1252" → xử lý file CSV lưu bởi Excel/Windows (chứa ký tự như ‘, ’, …)
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
            info(f"Đọc file với encoding: {enc}")
            break
        except (UnicodeDecodeError, Exception) as e:
            if enc == "latin-1":
                raise
            continue
    for col in ["shop_name", "listing_id", "title"]:
        if col not in df.columns:
            raise ValueError(f"CSV thiếu cột: '{col}'")
    info(f"Loaded {len(df)} rows từ {csv_path} (sep auto-detected)")
    return df

def extract_images(row: pd.Series) -> List[str]:
    urls = []
    for col in sorted(
        [c for c in row.index if re.match(r"^image_\d+$", c)],
        key=lambda x: int(x.split("_")[1])
    ):
        if row[col]:
            urls.append(row[col])
    return urls

def build_description(row: pd.Series) -> str:
    parts = []
    if row.get("tags"):
        parts.append(f"**Tags:** {row['tags']}")
    etsy = row.get("etsy_url") or row.get("url", "")
    if etsy:
        parts.append(f"**Etsy URL:** {etsy}")
    return "\n\n".join(parts)


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng sau card hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Main ──────────────────────────────────────────────────────────────────────
def upload_to_trello(
    csv_path: str = INPUT_CSV,
    board_id: str = TRELLO_BOARD_ID,
) -> None:
    """
    Upload/update heyetsy_image_urls.csv lên Trello.

    Logic mỗi card:
    - Card chưa tồn tại  → tạo mới + attach ảnh
    - Card đã tồn tại    → update title + desc
                           → xoá toàn bộ attachment cũ
                           → attach lại từ CSV
    """
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        sys.exit(1)

    setup_signal()
    df = load_csv(csv_path)

    info("Fetching existing Lists trên board...")
    existing_lists = fetch_existing_lists(board_id)
    info(f"  Board hiện có {len(existing_lists)} List(s).")

    existing_cards_cache: Dict[str, Dict[str, str]] = {}
    shops = df["shop_name"].unique().tolist()
    info(f"Tổng {len(df)} listings | {len(shops)} shops\n")

    cards_created   = 0
    cards_updated   = 0
    attachments_total = 0

    for shop_idx, shop_name in enumerate(shops, 1):
        if _interrupted:
            break

        shop_rows = df[df["shop_name"] == shop_name]
        info(f"[{shop_idx}/{len(shops)}] {shop_name}  ({len(shop_rows)} listings)")

        list_id = get_or_create_list(board_id, shop_name, existing_lists)

        # Fetch cards 1 lần / list
        if list_id not in existing_cards_cache:
            existing_cards_cache[list_id] = fetch_existing_cards(list_id)
        existing_cards = existing_cards_cache[list_id]

        for row_idx, (_, row) in enumerate(shop_rows.iterrows(), 1):
            if _interrupted:
                break

            card_name  = row.get("title") or row.get("listing_id", f"listing_{row_idx}")
            desc       = build_description(row)
            image_urls = extract_images(row)

            if card_name in existing_cards:
                # ── UPDATE card đã tồn tại ─────────────────────────────────
                card_id = existing_cards[card_name]

                update_card(card_id, card_name, desc)

                deleted = delete_all_attachments(card_id)

                attached = 0
                for url in image_urls:
                    if attach_url(card_id, url):
                        attached += 1
                    time.sleep(DELAY_ATTACHMENT)

                attachments_total += attached
                cards_updated += 1
                upd(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:50]}' "
                    f"— xoá {deleted} att cũ, thêm {attached}/{len(image_urls)} att mới")

            else:
                # ── TẠO card mới ──────────────────────────────────────────
                card_id = create_card(list_id, card_name, desc)
                existing_cards[card_name] = card_id
                cards_created += 1

                attached = 0
                for url in image_urls:
                    if attach_url(card_id, url):
                        attached += 1
                    time.sleep(DELAY_ATTACHMENT)

                attachments_total += attached
                done(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:50]}' "
                     f"— {attached}/{len(image_urls)} attachments")

            time.sleep(DELAY_CARD)

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("─" * 60)
    done("Upload hoàn tất!")
    info(f"  Cards tạo mới    : {cards_created}")
    info(f"  Cards cập nhật   : {cards_updated}")
    info(f"  Attachments tổng : {attachments_total}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    upload_to_trello()