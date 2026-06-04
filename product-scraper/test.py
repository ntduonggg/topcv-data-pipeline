"""
test_photo_on_shirt.py
──────────────────────
Test script: lấy 100 listing của PhotoOnShirt từ heyetsy_image_urls.csv,
download ảnh về máy rồi upload file lên Trello card (attachment = file).

Flow mỗi listing:
  1. Tạo card trong List đã có sẵn
  2. Download từng ảnh vào thư mục tạm
  3. Upload file lên card
  4. Xoá file tạm sau khi upload xong
"""

import os
import sys
import re
import time
import tempfile
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO  = "\033[94m"
    WARN  = "\033[93m"
    CKPT  = "\033[92m"
    ERROR = "\033[91m"
    TIME  = "\033[96m"
    DONE  = "\033[92m"
    SKIP  = "\033[90m"
    RESET = "\033[0m"

    @staticmethod
    def tag(color, label):
        return f"{color}[{label}]{C.RESET}"

def ts():
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {msg}")
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {msg}")
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,  'CKPT')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {msg}")
def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,  'SKIP')}  {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = ""
TRELLO_TOKEN    = ""
TRELLO_LIST_ID  = ""        # ID của List đã có sẵn trên board
# Cách lấy LIST_ID: mở board → thêm .json vào URL → tìm lists[].id

INPUT_CSV       = "heyetsy_image_urls.csv"
SHOP_FILTER     = "PhotoOnShirt"
LISTING_LIMIT   = 100       # số listing test

DELAY_CARD      = 0.3       # giây giữa mỗi card
DELAY_UPLOAD    = 0.5       # giây giữa mỗi file upload
DOWNLOAD_TIMEOUT = 15       # giây timeout download ảnh

TRELLO_BASE     = "https://api.trello.com/1"

# Card map để tránh duplicate (listing_id → card_id)
CARD_MAP_FILE   = "trello_card_map_test.json"


# ── Trello API helpers ────────────────────────────────────────────────────────
def _auth() -> Dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}

def _post_json(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _post_file(endpoint: str, filename: str, file_bytes: bytes, mime: str) -> dict:
    """Upload file binary lên Trello attachment."""
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        files={"file": (filename, file_bytes, mime)},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

def _get(endpoint: str, params: Dict = {}) -> dict | list:
    resp = requests.get(
        f"{TRELLO_BASE}{endpoint}",
        params={**_auth(), **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Card map (listing_id → card_id) ──────────────────────────────────────────
def load_card_map() -> Dict[str, str]:
    import json
    if not os.path.exists(CARD_MAP_FILE):
        return {}
    try:
        with open(CARD_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_card_map(card_map: Dict[str, str]) -> None:
    import json
    with open(CARD_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(card_map, f, indent=2, ensure_ascii=False)


# ── Trello operations ─────────────────────────────────────────────────────────
def create_card(list_id: str, name: str, desc: str = "") -> str:
    result = _post_json("/cards", {
        "idList": list_id,
        "name":   name,
        "desc":   desc,
        "pos":    "bottom",
    })
    return result["id"]

def fetch_existing_cards(list_id: str) -> Dict[str, str]:
    """Trả về {card_name → card_id} cho tất cả cards trong list."""
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {c["name"]: c["id"] for c in cards}

def upload_file_to_card(card_id: str, filename: str, file_bytes: bytes, mime: str) -> bool:
    """Upload file binary lên card attachment."""
    try:
        _post_file(f"/cards/{card_id}/attachments", filename, file_bytes, mime)
        return True
    except requests.HTTPError as e:
        warn(f"    Upload file thất bại ({filename}): {e}")
        return False


# ── Download image ────────────────────────────────────────────────────────────
def download_image(url: str) -> Optional[tuple]:
    """
    Download ảnh từ URL.
    Trả về (filename, bytes, mime_type) hoặc None nếu thất bại.
    """
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()

        # Lấy mime type
        mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

        # Lấy filename từ URL
        filename = url.split("/")[-1].split("?")[0]
        if not filename:
            ext = mime.split("/")[-1]
            filename = f"image.{ext}"

        return filename, resp.content, mime

    except Exception as e:
        warn(f"    Download thất bại ({url[:60]}...): {e}")
        return None


# ── CSV helpers ───────────────────────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
            info(f"Loaded {len(df)} rows từ {csv_path} (encoding={enc})")
            return df
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Không đọc được {csv_path}")

def extract_images(row: pd.Series) -> List[str]:
    return [
        row[c] for c in sorted(
            [c for c in row.index if re.match(r"^image_\d+$", c)],
            key=lambda x: int(x.split("_")[1])
        ) if row[c]
    ]

def build_description(row: pd.Series) -> str:
    if row.get("tags"):
        parts.append(f"**Tags:** {row['tags']}")
    etsy = row.get("etsy_url") or row.get("url", "")
    if etsy:
        parts.append(f"**Etsy URL:** {etsy}")
    return "\n\n".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not TRELLO_LIST_ID:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_LIST_ID!")
        sys.exit(1)

    # Load CSV
    df = load_csv(INPUT_CSV)

    # Lọc PhotoOnShirt, lấy tối đa LISTING_LIMIT dòng có ảnh
    df_shop = df[df["shop_name"] == SHOP_FILTER].copy()
    df_shop = df_shop[df_shop.get("image_1", pd.Series([""] * len(df_shop))) != ""]
    df_shop = df_shop.head(LISTING_LIMIT).reset_index(drop=True)

    info(f"Shop: {SHOP_FILTER} — {len(df_shop)} listings sẽ xử lý")

    if df_shop.empty:
        err(f"Không tìm thấy listing nào của {SHOP_FILTER} có ảnh.")
        sys.exit(1)

    # Load card map & fetch existing cards
    card_map = load_card_map()
    info("Fetching existing cards trong List...")
    existing_cards = fetch_existing_cards(TRELLO_LIST_ID)
    info(f"  List hiện có {len(existing_cards)} card(s).")

    total_cards_created  = 0
    total_cards_skipped  = 0
    total_images_uploaded = 0
    total_images_failed  = 0

    for i, (_, row) in enumerate(df_shop.iterrows(), 1):
        lid        = row.get("listing_id", "")
        card_name  = row.get("title") or lid
        desc       = build_description(row)
        image_urls = extract_images(row)

        info(f"[{i}/{len(df_shop)}] {lid} — {card_name[:60]}")

        # ── Kiểm tra đã tồn tại chưa (ưu tiên listing_id trong card_map) ─────
        if lid in card_map:
            card_id = card_map[lid]
            skip(f"  Đã có trong card_map — bỏ qua (card_id={card_id})")
            total_cards_skipped += 1
            continue

        if card_name in existing_cards:
            card_id = existing_cards[card_name]
            skip(f"  Title đã tồn tại trên Trello — bỏ qua (card_id={card_id})")
            card_map[lid] = card_id
            total_cards_skipped += 1
            continue

        # ── Tạo card mới ──────────────────────────────────────────────────────
        card_id = create_card(TRELLO_LIST_ID, card_name, desc)
        card_map[lid] = card_id
        existing_cards[card_name] = card_id
        total_cards_created += 1
        done(f"  Tạo card xong (id={card_id})")

        # ── Download & upload từng ảnh ────────────────────────────────────────
        uploaded = 0
        failed   = 0
        for j, url in enumerate(image_urls, 1):
            info(f"  [{j}/{len(image_urls)}] Download: {url[-50:]}")

            result = download_image(url)
            if result is None:
                failed += 1
                continue

            filename, file_bytes, mime = result
            info(f"    → {filename} ({len(file_bytes)//1024}KB) — uploading...")

            ok = upload_file_to_card(card_id, filename, file_bytes, mime)
            if ok:
                uploaded += 1
                done(f"    Upload OK")
            else:
                failed += 1

            time.sleep(DELAY_UPLOAD)

        total_images_uploaded += uploaded
        total_images_failed   += failed
        done(f"  {uploaded}/{len(image_urls)} ảnh upload thành công"
             + (f" | {failed} thất bại" if failed else ""))

        # Lưu card_map định kỳ sau mỗi card
        save_card_map(card_map)

        time.sleep(DELAY_CARD)

    # ── Summary ───────────────────────────────────────────────────────────────
    save_card_map(card_map)
    print(f"\n{'─'*55}")
    done("Hoàn tất!")
    info(f"  Cards tạo mới      : {total_cards_created}")
    info(f"  Cards skipped      : {total_cards_skipped}")
    info(f"  Ảnh upload thành công : {total_images_uploaded}")
    info(f"  Ảnh thất bại       : {total_images_failed}")
    info(f"  Card map saved     : {CARD_MAP_FILE}")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    run()