"""
find_hidden_listings.py
────────────────────────
Flow:
  STEP 1: Load reviews.csv
  STEP 2: Extract unique product names (data4 ưu tiên, fallback data3)
  STEP 3: Load everbee_shop_data_listings.csv
  STEP 4: Match title → lấy listing_id
  STEP 5: Truy cập etsy.com/listing/{id} → check "unavailable"
          → listing unavailable đưa vào hidden_listings.csv
  STEP 6: Upload lên Trello (1 List/shop, 1 card/listing)
"""

import os
import sys
import re
import time
import signal
import requests
import pandas as pd
from bs4 import BeautifulSoup
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
REVIEWS_CSV  = "reviews.csv"
EVERBEE_CSV  = "everbee_shop_data_listings.csv"
OUTPUT_CSV   = "hidden_listings.csv"

REVIEW_NAME_COL_PRIMARY  = "data4"
REVIEW_NAME_COL_FALLBACK = "data3"

# Etsy check
ETSY_LISTING_URL = "https://www.etsy.com/listing/{id}"
REQUEST_TIMEOUT  = 15
DELAY_BETWEEN    = 1.0   # giây giữa mỗi request Etsy (tránh rate limit)

# Các chuỗi xuất hiện trên trang listing không còn tồn tại / unavailable
UNAVAILABLE_SIGNALS = [
    "is unavailable",
    "no longer available",
    "this listing has expired",
    "page not found",
    "this shop is on vacation",
    "sold out",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Trello
TRELLO_API_KEY   = ""
TRELLO_TOKEN     = ""
TRELLO_BOARD_ID  = ""
TRELLO_LIST_ID   = ""
TRELLO_LIST_NAME = ""   # rỗng → tự lấy shop_name từ everbee CSV

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


# ── Step 2: Extract product names ─────────────────────────────────────────────
def extract_product_names(df_reviews: pd.DataFrame) -> List[str]:
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


# ── Step 4: Match title → listing_id ──────────────────────────────────────────
def match_listing_ids(
    product_names: List[str],
    df_everbee:    pd.DataFrame,
) -> List[Dict]:
    """
    Match exact product name với title trong everbee CSV.
    Trả về list [{listing_id, title, etsy_url}].
    """
    title_col = next(
        (c for c in ["title", "Title", "name", "Name"] if c in df_everbee.columns),
        None
    )
    id_col = next(
        (c for c in ["listing_id", "id"] if c in df_everbee.columns),
        None
    )
    url_col = next(
        (c for c in ["etsy_url", "url", "URL"] if c in df_everbee.columns),
        None
    )

    info(f"Columns detected — title:{title_col} id:{id_col} url:{url_col}")

    if not title_col or not id_col:
        raise ValueError("everbee CSV thiếu cột title hoặc listing_id")

    title_map: Dict[str, pd.Series] = {}
    for _, row in df_everbee.iterrows():
        t = str(row.get(title_col, "")).strip()
        if t:
            title_map[t] = row

    matched      = []
    not_found    = []

    for name in product_names:
        row = title_map.get(name)
        if row is None:
            not_found.append(name)
            continue

        listing_id = str(row.get(id_col, "")).strip()
        etsy_url   = str(row.get(url_col, "")).strip() if url_col else ""

        if not etsy_url:
            etsy_url = ETSY_LISTING_URL.format(id=listing_id)

        matched.append({
            "listing_id": listing_id,
            "title":      name,
            "etsy_url":   etsy_url,
        })

    info(f"Matched: {len(matched)} | Không tìm thấy: {len(not_found)}")
    if not_found:
        for n in not_found[:10]:
            warn(f"  Không match: '{n[:60]}'")
        if len(not_found) > 10:
            warn(f"  ... và {len(not_found) - 10} listing khác")

    return matched


# ── Step 5: Check Etsy availability ───────────────────────────────────────────
def check_etsy_unavailable(url: str) -> Optional[bool]:
    """
    Truy cập trang Etsy listing, check signal "unavailable".
    Trả về:
      True  → listing unavailable (bị ẩn/xóa)
      False → listing vẫn active
      None  → lỗi request, không xác định được
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as e:
        warn(f"  Request lỗi: {e}")
        return None

    # Etsy redirect về trang search/category khi listing không tồn tại
    if "/listing/" not in resp.url:
        return True

    if resp.status_code == 404:
        return True

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()

    for signal_str in UNAVAILABLE_SIGNALS:
        if signal_str in page_text:
            return True

    return False


def find_hidden_listings(matched: List[Dict]) -> pd.DataFrame:
    """
    Duyệt từng listing đã match, check Etsy.
    Trả về DataFrame các listing unavailable.
    """
    hidden = []
    error_list = []

    for i, item in enumerate(matched, 1):
        lid   = item["listing_id"]
        title = item["title"]
        url   = item["etsy_url"]

        info(f"[{i}/{len(matched)}] Checking listing {lid} — '{title[:50]}'")

        result = check_etsy_unavailable(url)

        if result is None:
            warn(f"  Lỗi request — bỏ qua")
            error_list.append(item)
        elif result is True:
            done(f"  UNAVAILABLE — '{title[:50]}'")
            hidden.append(item)
        else:
            skip(f"  Active — bỏ qua")

        time.sleep(DELAY_BETWEEN)

    info(f"\nKết quả: {len(hidden)} unavailable | "
         f"{len(error_list)} lỗi request | "
         f"{len(matched) - len(hidden) - len(error_list)} active")

    return pd.DataFrame(hidden) if hidden else pd.DataFrame(
        columns=["listing_id", "title", "etsy_url"]
    )


# ── Step: Save output ─────────────────────────────────────────────────────────
def save_output(df: pd.DataFrame, path: str = OUTPUT_CSV) -> None:
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Saved → {path} ({len(df)} listings)")


# ── Step 6: Upload Trello ─────────────────────────────────────────────────────
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
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    return {lst["name"]: lst["id"] for lst in lists}

def create_list(board_id: str, name: str) -> str:
    return _post("/lists", {"name": name, "idBoard": board_id, "pos": "bottom"})["id"]

def get_or_create_list(board_id: str, list_id: str, list_name: str) -> str:
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
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {c["name"]: c["id"] for c in cards}

def create_card(list_id: str, name: str, desc: str = "") -> str:
    return _post("/cards", {
        "idList": list_id, "name": name, "desc": desc, "pos": "bottom",
    })["id"]

def attach_url(card_id: str, url: str) -> bool:
    try:
        _post(f"/cards/{card_id}/attachments", {"url": url})
        return True
    except requests.HTTPError as e:
        warn(f"  Attachment thất bại: {e}")
        return False

def build_card_desc(row: Dict) -> str:
    parts = [f"**Listing ID:** {row['listing_id']}"]
    if row.get("etsy_url"):
        parts.append(f"**Etsy URL:** {row['etsy_url']}")
    parts.append("**Status:** Unavailable")
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

    resolved_name = list_name or TRELLO_LIST_NAME or "Hidden Listings"
    info(f"Tên List sẽ dùng: '{resolved_name}'")

    list_id = get_or_create_list(TRELLO_BOARD_ID, TRELLO_LIST_ID, resolved_name)

    info("Fetching existing cards trong List...")
    existing = fetch_existing_cards(list_id)
    info(f"  List hiện có {len(existing)} card(s).")

    created = 0
    skipped = 0

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
    reviews_csv: str = REVIEWS_CSV,
    everbee_csv: str = EVERBEE_CSV,
    output_csv:  str = OUTPUT_CSV,
) -> pd.DataFrame:
    setup_signal()

    sep()
    info("STEP 1 — Load reviews.csv")
    df_reviews = load_csv(reviews_csv, "reviews")

    sep()
    info("STEP 2 — Extract unique product names (data4 ưu tiên, fallback data3)")
    product_names = extract_product_names(df_reviews)

    sep()
    info("STEP 3 — Load everbee_shop_data_listings.csv")
    df_everbee = load_csv(everbee_csv, "everbee listings")

    # Lấy shop_name cho Trello list name
    shop_name = ""
    shop_col  = next((c for c in ["shop_name", "shop", "Shop"] if c in df_everbee.columns), None)
    if shop_col:
        names = [n.strip() for n in df_everbee[shop_col].dropna().unique().tolist() if str(n).strip()]
        if names:
            shop_name = names[0]
            info(f"Shop name: '{shop_name}'")

    sep()
    info("STEP 4 — Match title → listing_id")
    matched = match_listing_ids(product_names, df_everbee)

    sep()
    info("STEP 5 — Check Etsy availability")
    df_hidden = find_hidden_listings(matched)

    sep()
    info("STEP 5b — Save hidden_listings.csv")
    save_output(df_hidden, output_csv)

    sep()
    info("STEP 6 — Upload lên Trello")
    upload_to_trello(df_hidden, list_name=shop_name)

    sep()
    done("Pipeline hoàn tất!")
    info(f"  Product names từ reviews : {len(product_names)}")
    info(f"  Matched với everbee       : {len(matched)}")
    info(f"  Listings unavailable      : {len(df_hidden)}")
    info(f"  Output CSV                : {output_csv}")
    sep()

    return df_hidden


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()