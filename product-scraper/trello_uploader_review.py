"""
find_hidden_listings.py
────────────────────────
So sánh 2 file CSV:
  1. reviews.csv           — chứa tên sản phẩm đã từng có review (data4, fallback data3)
  2. everbee_listings.csv  — chứa toàn bộ listings của shop (title, state, review_count, ...)

Logic:
  - Lấy unique product names từ reviews.csv
  - Match exact với title trong everbee_listings.csv
  - Filter: state != "active" (đã bị ẩn/xóa) VÀ review_count > 0
  - Upload listing_id tìm được lên Trello (1 card / listing)

Output:
  - hidden_listings.csv  — danh sách listing bị ẩn
  - Upload lên Trello List có sẵn
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
def sep():      print("─" * 60)


# ── Config ────────────────────────────────────────────────────────────────────
REVIEWS_CSV   = "reviews.csv"
EVERBEE_CSV   = "everbee_shop_data_listings.csv"
OUTPUT_CSV    = "hidden_listings.csv"

# Cột tên sản phẩm trong reviews.csv (ưu tiên data4, fallback data3)
REVIEW_NAME_COL_PRIMARY   = "data4"
REVIEW_NAME_COL_FALLBACK  = "data3"

# Cột tương ứng trong everbee_listings.csv
EVERBEE_TITLE_COL         = "title"
EVERBEE_STATE_COL         = "state"
EVERBEE_REVIEW_COUNT_COL  = "review_count"
EVERBEE_LISTING_ID_COL    = "listing_id"
EVERBEE_URL_COL           = "url"          # hoặc "etsy_url"

# Trello
TRELLO_API_KEY  = os.abort("TRELLO_API_KEY not set in environment") if "TRELLO_API_KEY" not in os.environ else os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN    = os.abort("TRELLO_TOKEN not set in environment") if "TRELLO_TOKEN" not in os.environ else os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.abort("TRELLO_BOARD_ID not set in environment") if "TRELLO_BOARD_ID" not in os.environ else os.getenv("TRELLO_BOARD_ID")
TRELLO_LIST_ID   = ""              # Nếu có sẵn thì điền, để rỗng để tạo mới
TRELLO_LIST_NAME = ""   # Để rỗng → tự lấy từ shop_name trong everbee CSV

DELAY_CARD       = 0.3
DELAY_ATTACHMENT = 0.2
TRELLO_BASE      = "https://api.trello.com/1"


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str, label: str = "") -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(
                csv_path, dtype=str, sep=None,
                engine="python", encoding=enc
            ).fillna("")
            info(f"Loaded {label or csv_path}: {len(df)} rows (encoding={enc})")
            return df
        except UnicodeDecodeError:
            if enc == "latin-1":
                raise
            continue
    raise RuntimeError(f"Không đọc được {csv_path}")


# ── Step 1: Extract product names từ reviews.csv ──────────────────────────────
def extract_product_names(df_reviews: pd.DataFrame) -> List[str]:
    """
    Lấy unique product names từ reviews CSV.
    Ưu tiên data4, fallback sang data3 nếu data4 rỗng.
    """
    names = set()
    for _, row in df_reviews.iterrows():
        name = str(row.get(REVIEW_NAME_COL_PRIMARY, "")).strip()
        if not name:
            name = str(row.get(REVIEW_NAME_COL_FALLBACK, "")).strip()
        if name:
            names.add(name)

    result = sorted(names)
    info(f"Unique product names từ reviews: {len(result)}")
    return result


# ── Step 2: Match với everbee listings ───────────────────────────────────────
def find_hidden_listings(
    product_names: List[str],
    df_everbee:    pd.DataFrame,
) -> pd.DataFrame:
    """
    Match product names (exact) với title trong everbee CSV.
    Filter: state != "active" AND review_count > 0.
    Trả về DataFrame các listing bị ẩn.
    """
    # Normalize title để match
    title_col = EVERBEE_TITLE_COL
    if title_col not in df_everbee.columns:
        # Fallback column names
        for alt in ["Title", "name", "Name"]:
            if alt in df_everbee.columns:
                title_col = alt
                break

    state_col  = next((c for c in [EVERBEE_STATE_COL, "State", "status"]
                       if c in df_everbee.columns), None)
    review_col = next((c for c in [EVERBEE_REVIEW_COUNT_COL, "review_count", "reviews"]
                       if c in df_everbee.columns), None)
    id_col     = next((c for c in [EVERBEE_LISTING_ID_COL, "listing_id", "id"]
                       if c in df_everbee.columns), None)
    url_col    = next((c for c in [EVERBEE_URL_COL, "etsy_url", "url", "URL"]
                       if c in df_everbee.columns), None)

    info(f"Columns detected — title:{title_col} state:{state_col} "
         f"review:{review_col} id:{id_col} url:{url_col}")

    # Build lookup: {normalized_title → row}
    title_map: Dict[str, pd.Series] = {}
    for _, row in df_everbee.iterrows():
        t = str(row.get(title_col, "")).strip()
        if t:
            title_map[t] = row

    hidden = []
    not_found_in_everbee = []

    for name in product_names:
        row = title_map.get(name)

        if row is None:
            not_found_in_everbee.append(name)
            warn(f"  Không tìm thấy trong everbee: '{name[:60]}'")
            continue

        # Kiểm tra state
        state = str(row.get(state_col, "")).strip().lower() if state_col else ""
        if state == "active":
            skip(f"  Active — bỏ qua: '{name[:60]}'")
            continue

        # Kiểm tra review_count > 0
        review_count = 0
        if review_col:
            try:
                review_count = int(str(row.get(review_col, "0")).strip() or "0")
            except ValueError:
                review_count = 0

        # if review_count <= 0:
        #     skip(f"  review_count=0 — bỏ qua: '{name[:60]}'")
        #     continue

        # Listing bị ẩn và có review
        listing_id = str(row.get(id_col, "")).strip() if id_col else ""
        etsy_url   = str(row.get(url_col, "")).strip() if url_col else ""

        done(f"  HIDDEN | state='{state}' | reviews={review_count} | '{name[:55]}'")
        hidden.append({
            "listing_id":   listing_id,
            "title":        name,
            "state":        state,
            "review_count": review_count,
            "etsy_url":     etsy_url,
        })

    info(f"\nKết quả: {len(hidden)} listing bị ẩn | "
         f"{len(not_found_in_everbee)} không tìm thấy trong everbee")

    return pd.DataFrame(hidden) if hidden else pd.DataFrame(
        columns=["listing_id", "title", "state", "review_count", "etsy_url"]
    )


# ── Step 3: Lưu output CSV ────────────────────────────────────────────────────
def save_output(df: pd.DataFrame, path: str = OUTPUT_CSV) -> None:
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Saved → {path} ({len(df)} listings)")


# ── Step 4: Upload lên Trello ─────────────────────────────────────────────────
def _auth() -> Dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}

def _get(endpoint: str, params: Dict = {}) -> dict | list:
    resp = requests.get(f"{TRELLO_BASE}{endpoint}", params={**_auth(), **params}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _post(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.post(f"{TRELLO_BASE}{endpoint}", params={**_auth(), **payload}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def fetch_board_lists(board_id: str) -> Dict[str, str]:
    """Trả về {list_name → list_id} cho tất cả lists trên board."""
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    return {lst["name"]: lst["id"] for lst in lists}

def create_list(board_id: str, name: str) -> str:
    """Tạo List mới trong board, trả về list_id."""
    result = _post("/lists", {"name": name, "idBoard": board_id, "pos": "bottom"})
    return result["id"]

def get_or_create_list(board_id: str, list_id: str, list_name: str) -> str:
    """
    Trả về list_id để dùng:
    - Nếu TRELLO_LIST_ID đã điền → dùng luôn
    - Nếu rỗng → tìm list theo TRELLO_LIST_NAME trên board
    - Nếu không tìm thấy → tạo mới
    """
    if list_id:
        info(f"Dùng List có sẵn: {list_id}")
        return list_id

    if not board_id:
        raise ValueError("Cần điền TRELLO_BOARD_ID hoặc TRELLO_LIST_ID!")

    existing = fetch_board_lists(board_id)
    if list_name in existing:
        lid = existing[list_name]
        info(f"Tìm thấy List '{list_name}' (id={lid})")
        return lid

    lid = create_list(board_id, list_name)
    done(f"Tạo List mới '{list_name}' (id={lid})")
    return lid

def fetch_existing_cards(list_id: str) -> Dict[str, str]:
    """Trả về {card_name → card_id}."""
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {c["name"]: c["id"] for c in cards}

def create_card(list_id: str, name: str, desc: str = "") -> str:
    return _post("/cards", {
        "idList": list_id,
        "name":   name,
        "desc":   desc,
        "pos":    "bottom",
    })["id"]

def attach_url(card_id: str, url: str) -> bool:
    try:
        _post(f"/cards/{card_id}/attachments", {"url": url})
        return True
    except requests.HTTPError as e:
        warn(f"  Attachment thất bại: {e}")
        return False

def build_card_desc(row: Dict) -> str:
    parts = [
        f"**Listing ID:** {row['listing_id']}",
        f"**State:** {row['state']}",
        f"**Reviews:** {row['review_count']}",
    ]
    if row.get("etsy_url"):
        parts.append(f"**Etsy URL:** {row['etsy_url']}")
    return "\n\n".join(parts)

def upload_to_trello(df_hidden: pd.DataFrame, list_name: str = "") -> None:
    if df_hidden.empty:
        info("Không có listing nào để upload.")
        return

    if not TRELLO_API_KEY or not TRELLO_TOKEN:
        warn("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN — bỏ qua bước upload.")
        return

    if not TRELLO_LIST_ID and not TRELLO_BOARD_ID:
        warn("Chưa điền TRELLO_LIST_ID hoặc TRELLO_BOARD_ID — bỏ qua bước upload.")
        return

    # Tên list: ưu tiên shop_name từ CSV, fallback config, fallback mặc định
    resolved_name = list_name or TRELLO_LIST_NAME or "Hidden Listings"
    info(f"Tên List sẽ dùng: '{resolved_name}'")

    # Get hoặc tạo list
    list_id = get_or_create_list(TRELLO_BOARD_ID, TRELLO_LIST_ID, resolved_name)

    info("Fetching existing cards trong List...")
    existing = fetch_existing_cards(list_id)
    info(f"  List hiện có {len(existing)} card(s).")

    created  = 0
    skipped  = 0

    for _, row in df_hidden.iterrows():
        lid       = row.get("listing_id", "")
        title     = row.get("title", lid)
        card_name = f"{lid} — {title[:60]}" if lid else title[:80]
        desc      = build_card_desc(dict(row))
        etsy_url  = row.get("etsy_url", "")

        if card_name in existing:
            skip(f"  Card '{card_name[:60]}' đã tồn tại — skip")
            skipped += 1
            continue

        card_id = create_card(list_id, card_name, desc)

        if etsy_url:
            attach_url(card_id, etsy_url)
            time.sleep(DELAY_ATTACHMENT)

        done(f"  Tạo card: '{card_name[:60]}'")
        created += 1
        time.sleep(DELAY_CARD)

    sep()
    done("Upload Trello hoàn tất!")
    info(f"  Cards tạo mới : {created}")
    info(f"  Cards skipped : {skipped}")


# ── Signal handler ────────────────────────────────────────────────────────────
_interrupted = False

def setup_signal():
    def _handler(sig, frame):
        global _interrupted
        stop("Ctrl+C — dừng...")
        _interrupted = True
    signal.signal(signal.SIGINT, _handler)


# ── Main ──────────────────────────────────────────────────────────────────────
def run(
    reviews_csv:  str = REVIEWS_CSV,
    everbee_csv:  str = EVERBEE_CSV,
    output_csv:   str = OUTPUT_CSV,
) -> pd.DataFrame:
    setup_signal()

    sep()
    info("STEP 1 — Load reviews CSV")
    df_reviews = load_csv(reviews_csv, "reviews")

    sep()
    info("STEP 2 — Extract unique product names")
    product_names = extract_product_names(df_reviews)

    sep()
    info("STEP 3 — Load everbee listings CSV")
    df_everbee = load_csv(everbee_csv, "everbee listings")

    # Lấy tên shop từ cột shop_name trong everbee CSV
    shop_name = ""
    shop_col  = next((c for c in ["shop_name", "shop", "Shop"] if c in df_everbee.columns), None)
    if shop_col:
        names = df_everbee[shop_col].dropna().unique().tolist()
        names = [n.strip() for n in names if str(n).strip()]
        if names:
            shop_name = names[0]
            info(f"Shop name từ everbee CSV: '{shop_name}'")
        if len(names) > 1:
            warn(f"Nhiều hơn 1 shop trong CSV: {names} — dùng shop đầu tiên: '{shop_name}'"
            )
    if not shop_name:
        shop_name = "Hidden Listings"
        warn(f"Không tìm thấy shop_name trong CSV — dùng mặc định: '{shop_name}'"
        )

    sep()
    info("STEP 4 — Match & filter hidden listings")
    df_hidden = find_hidden_listings(product_names, df_everbee)

    sep()
    info("STEP 5 — Save output CSV")
    save_output(df_hidden, output_csv)

    sep()
    info("STEP 6 — Upload lên Trello")
    upload_to_trello(df_hidden, list_name=shop_name)

    sep()
    done("Pipeline hoàn tất!")
    info(f"  Tổng product names từ reviews : {len(product_names)}")
    info(f"  Listings bị ẩn tìm được       : {len(df_hidden)}")
    info(f"  Output CSV                     : {output_csv}")
    sep()

    return df_hidden


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()