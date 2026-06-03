import os
import sys
import re
import time
import signal
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO      = "\033[94m"
    ACTION    = "\033[94m"
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


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY   = ""
TRELLO_TOKEN     = ""
TRELLO_BOARD_ID  = ""

INPUT_CSV        = "heyetsy_image_urls.csv"

# Delay giữa các API call (tránh 429 Trello)
DELAY_BETWEEN_CARDS       = 0.3   # giây giữa mỗi card
DELAY_BETWEEN_ATTACHMENTS = 0.2   # giây giữa mỗi attachment
DELAY_BETWEEN_LISTS       = 1.0   # giây giữa mỗi list mới

TRELLO_BASE = "https://api.trello.com/1"


# ── Hướng dẫn lấy BOARD_ID ───────────────────────────────────────────────────
# Mở board trên Trello → thêm ".json" vào cuối URL → tìm field "id"
# Ví dụ: https://trello.com/b/AbCdEfGh/ten-board.json


# ── Trello API helpers ────────────────────────────────────────────────────────
def _auth_params() -> Dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}


def _get(endpoint: str, params: Dict = {}) -> dict | list:
    resp = requests.get(
        f"{TRELLO_BASE}{endpoint}",
        params={**_auth_params(), **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth_params(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Fetch existing Lists & Cards ──────────────────────────────────────────────
def fetch_existing_lists(board_id: str) -> Dict[str, str]:
    """
    Trả về dict {list_name → list_id} cho tất cả Lists hiện có trên board.
    """
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    return {lst["name"]: lst["id"] for lst in lists}


def fetch_existing_cards(list_id: str) -> Dict[str, str]:
    """
    Trả về dict {card_name → card_id} cho tất cả Cards trong 1 List.
    Dùng để skip card đã tồn tại.
    """
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {card["name"]: card["id"] for card in cards}


# ── Create List / Card / Attachment ──────────────────────────────────────────
def create_list(board_id: str, name: str) -> str:
    """Tạo List mới trong board, trả về list_id."""
    result = _post("/lists", {"name": name, "idBoard": board_id, "pos": "bottom"})
    return result["id"]


def create_card(list_id: str, name: str, description: str = "") -> str:
    """Tạo Card trong list, trả về card_id."""
    result = _post("/cards", {
        "idList":  list_id,
        "name":    name,
        "desc":    description,
        "pos":     "bottom",
    })
    return result["id"]


def attach_url_to_card(card_id: str, url: str, name: str = "") -> bool:
    """
    Đính kèm URL ảnh vào card dạng Attachment.
    Trello tự preview ảnh nếu URL trả về content-type image/*.
    Trả về True nếu thành công.
    """
    try:
        _post(f"/cards/{card_id}/attachments", {"url": url, "name": name})
        return True
    except requests.HTTPError as e:
        warn(f"  Attachment thất bại ({url[:60]}...): {e}")
        return False


# ── Load CSV ──────────────────────────────────────────────────────────────────
def load_image_csv(csv_path: str) -> pd.DataFrame:
    """
    Đọc output CSV từ heyetsy_image_downloader.
    Cột bắt buộc: shop_name, listing_id, title, tags, image_1, image_2, ...
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    for col in ["shop_name", "listing_id", "title"]:
        if col not in df.columns:
            raise ValueError(f"CSV thiếu cột bắt buộc: '{col}'")
    info(f"Loaded {len(df)} rows từ {csv_path}")
    return df


def extract_image_urls(row: pd.Series) -> List[str]:
    """Trích xuất tất cả image_N từ 1 row, bỏ qua ô rỗng."""
    urls = []
    for col in sorted(row.index, key=lambda x: int(x.split("_")[1]) if re.match(r"^image_\d+$", x) else 0):
        if re.match(r"^image_\d+$", col) and row[col]:
            urls.append(row[col])
    return urls


def build_card_description(row: pd.Series) -> str:
    """
    Description của card:
    - listing_id
    - tags
    - etsy URL (nếu có)
    """
    parts = []
    if row.get("tags"):
        parts.append(f"**Tags:** {row['tags']}")
    if row.get("url") or row.get("etsy_url"):
        etsy = row.get("url") or row.get("etsy_url")
        parts.append(f"**Etsy URL:** {etsy}")
    return "\n\n".join(parts)


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — sẽ dừng sau listing hiện tại...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Main upload ───────────────────────────────────────────────────────────────
def upload_to_trello(
    csv_path: str  = INPUT_CSV,
    board_id: str  = TRELLO_BOARD_ID,
) -> None:
    """
    Entry point chính.
    Đọc heyetsy_image_urls.csv → upload lên Trello:
      - 1 List  / shop
      - 1 Card  / listing  (title = card name, tags = description)
      - N Attachments / card (image URLs)
    Skip List/Card đã tồn tại.
    """
    # ── Validate credentials ───────────────────────────────────────────────────
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        err("Xem hướng dẫn trong phần Config của file này.")
        sys.exit(1)

    setup_signal()

    df = load_image_csv(csv_path)

    # ── Fetch tất cả Lists hiện có (1 lần) ────────────────────────────────────
    info("Fetching existing Lists trên board...")
    existing_lists: Dict[str, str] = fetch_existing_lists(board_id)
    info(f"  Board hiện có {len(existing_lists)} List(s).")

    # Cache cards theo list_id để tránh gọi API lặp lại
    existing_cards_cache: Dict[str, Dict[str, str]] = {}

    # ── Group theo shop ────────────────────────────────────────────────────────
    shops = df["shop_name"].unique().tolist()
    info(f"Tổng {len(df)} listings | {len(shops)} shops → bắt đầu upload...\n")

    total_cards_created      = 0
    total_cards_skipped      = 0
    total_attachments_added  = 0

    for shop_idx, shop_name in enumerate(shops, 1):
        if _interrupted:
            break

        shop_rows = df[df["shop_name"] == shop_name]
        info(f"[{shop_idx}/{len(shops)}] Shop: {shop_name}  ({len(shop_rows)} listings)")

        # ── Get hoặc tạo List ──────────────────────────────────────────────────
        if shop_name in existing_lists:
            list_id = existing_lists[shop_name]
            skip(f"  List '{shop_name}' đã tồn tại — dùng lại (id={list_id})")
        else:
            list_id = create_list(board_id, shop_name)
            existing_lists[shop_name] = list_id
            done(f"  Tạo List '{shop_name}' (id={list_id})")
            time.sleep(DELAY_BETWEEN_LISTS)

        # ── Fetch existing cards của list này (1 lần / list) ──────────────────
        if list_id not in existing_cards_cache:
            existing_cards_cache[list_id] = fetch_existing_cards(list_id)
        existing_cards = existing_cards_cache[list_id]

        # ── Tạo card cho từng listing ──────────────────────────────────────────
        for row_idx, (_, row) in enumerate(shop_rows.iterrows(), 1):
            if _interrupted:
                break

            card_name = row.get("title", "") or row.get("listing_id", f"listing_{row_idx}")
            lid       = row.get("listing_id", "")

            # Skip nếu card đã tồn tại
            if card_name in existing_cards:
                skip(f"  [{row_idx}/{len(shop_rows)}] Card '{card_name[:50]}' đã tồn tại — bỏ qua")
                total_cards_skipped += 1
                continue

            # Tạo card
            desc    = build_card_description(row)
            card_id = create_card(list_id, card_name, desc)
            existing_cards[card_name] = card_id  # cập nhật cache
            total_cards_created += 1

            # Đính kèm image URLs
            image_urls = extract_image_urls(row)
            attached   = 0
            for img_url in image_urls:
                ok = attach_url_to_card(card_id, img_url)
                if ok:
                    attached += 1
                time.sleep(DELAY_BETWEEN_ATTACHMENTS)

            total_attachments_added += attached
            done(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:50]}' "
                 f"— {attached}/{len(image_urls)} attachments")

            time.sleep(DELAY_BETWEEN_CARDS)

        print()  # blank line giữa các shop

    # ── Summary ────────────────────────────────────────────────────────────────
    print("─" * 60)
    done(f"Upload hoàn tất!")
    info(f"  Cards tạo mới : {total_cards_created}")
    info(f"  Cards skipped : {total_cards_skipped}")
    info(f"  Attachments   : {total_attachments_added}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C — các listing còn lại chưa upload)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    upload_to_trello()