"""
upload_to_trello.py
───────────────────
Bước 2: Đọc ảnh từ folder images/ → upload file lên Trello.

Đọc cấu trúc folder:
  images/
    PhotoOnShirt/
      1696869276/
        image_1.jpg
        image_2.jpg
      ...

Lookup metadata (title, tags, etsy_url) từ heyetsy_image_urls.csv theo listing_id.
Dùng trello_card_map.json để tránh tạo duplicate card.
"""

import os
import re
import sys
import time
import json
import mimetypes
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
def upd(msg):   print(f"{ts()} {C.tag(C.CKPT,  'UPD')}   {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
TRELLO_API_KEY  = ""
TRELLO_TOKEN    = ""
TRELLO_LIST_ID  = ""       # ID của List đã có sẵn trên board

INPUT_CSV       = "heyetsy_image_urls.csv"
IMAGES_DIR      = "images"             # phải khớp với OUTPUT_DIR của download_images.py
CARD_MAP_FILE   = "trello_card_map.json"

DELAY_CARD      = 0.3    # giây giữa các card
DELAY_UPLOAD    = 0.5    # giây giữa mỗi file upload

TRELLO_BASE     = "https://api.trello.com/1"


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

def _post_json(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params=_auth(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _post_file(endpoint: str, filename: str, file_bytes: bytes, mime: str) -> bool:
    try:
        resp = requests.post(
            f"{TRELLO_BASE}{endpoint}",
            params=_auth(),
            files={"file": (filename, file_bytes, mime)},
            timeout=60,
        )
        resp.raise_for_status()
        return True
    except requests.HTTPError as e:
        warn(f"    Upload thất bại ({filename}): {e}")
        return False


# ── Card map ──────────────────────────────────────────────────────────────────
def load_card_map() -> Dict[str, str]:
    if not os.path.exists(CARD_MAP_FILE):
        return {}
    try:
        with open(CARD_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_card_map(card_map: Dict[str, str]) -> None:
    with open(CARD_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(card_map, f, indent=2, ensure_ascii=False)


# ── Trello operations ─────────────────────────────────────────────────────────
def fetch_existing_cards(list_id: str) -> Dict[str, str]:
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id,name"})
    return {c["name"]: c["id"] for c in cards}

def create_card(list_id: str, name: str, desc: str = "") -> str:
    result = _post_json("/cards", {
        "idList": list_id,
        "name":   name,
        "desc":   desc,
        "pos":    "bottom",
    })
    return result["id"]

def upload_file(card_id: str, file_path: str) -> bool:
    filename = os.path.basename(file_path)
    mime, _  = mimetypes.guess_type(file_path)
    mime     = mime or "image/jpeg"
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    return _post_file(f"/cards/{card_id}/attachments", filename, file_bytes, mime)


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

def build_meta_map(df: pd.DataFrame) -> Dict[str, Dict]:
    """Tạo dict {listing_id → {title, tags, etsy_url}} để lookup nhanh."""
    meta = {}
    for _, row in df.iterrows():
        lid = row.get("listing_id", "")
        if lid:
            meta[lid] = {
                "title":    row.get("title", "") or lid,
                "tags":     row.get("tags", ""),
                "etsy_url": row.get("etsy_url") or row.get("url", ""),
            }
    return meta

def build_description(meta: Dict) -> str:
    parts = []
    if meta.get("tags"):
        parts.append(f"**Tags:** {meta['tags']}")
    if meta.get("etsy_url"):
        parts.append(f"**Etsy URL:** {meta['etsy_url']}")
    return "\n\n".join(parts)


# ── Scan images folder ────────────────────────────────────────────────────────
def scan_listings(images_dir: str) -> List[Dict]:
    """
    Quét folder images/{shop}/{listing_id}/ → trả về list:
    [{shop_name, listing_id, image_files: [path1, path2, ...]}, ...]
    Sắp xếp image_files theo thứ tự số (image_1, image_2, ...).
    """
    listings = []
    if not os.path.isdir(images_dir):
        return listings

    for shop_name in sorted(os.listdir(images_dir)):
        shop_dir = os.path.join(images_dir, shop_name)
        if not os.path.isdir(shop_dir):
            continue
        for listing_id in sorted(os.listdir(shop_dir)):
            lid_dir = os.path.join(shop_dir, listing_id)
            if not os.path.isdir(lid_dir):
                continue
            # Lấy file ảnh, sắp xếp theo số trong tên
            files = sorted(
                [os.path.join(lid_dir, f) for f in os.listdir(lid_dir)
                 if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))],
                key=lambda p: int(re.search(r"\d+", os.path.basename(p)).group())
                if re.search(r"\d+", os.path.basename(p)) else 0
            )
            if files:
                listings.append({
                    "shop_name":   shop_name,
                    "listing_id":  listing_id,
                    "image_files": files,
                })
    return listings


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not TRELLO_LIST_ID:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_LIST_ID!")
        sys.exit(1)

    # Load metadata từ CSV
    df       = load_csv(INPUT_CSV)
    meta_map = build_meta_map(df)

    # Scan folder ảnh
    listings = scan_listings(IMAGES_DIR)
    info(f"Tìm thấy {len(listings)} listing folders trong '{IMAGES_DIR}/'")

    if not listings:
        err(f"Không tìm thấy ảnh trong '{IMAGES_DIR}/' — hãy chạy download_images.py trước.")
        sys.exit(1)

    # Load card map & existing cards
    card_map       = load_card_map()
    existing_cards = fetch_existing_cards(TRELLO_LIST_ID)
    info(f"List hiện có {len(existing_cards)} card(s) | card_map: {len(card_map)} entries")

    total_created  = 0
    total_skipped  = 0
    total_uploaded = 0
    total_failed   = 0

    for i, listing in enumerate(listings, 1):
        lid        = listing["listing_id"]
        shop_name  = listing["shop_name"]
        files      = listing["image_files"]
        meta       = meta_map.get(lid, {})
        card_name  = meta.get("title") or lid
        desc       = build_description(meta)

        info(f"[{i}/{len(listings)}] {lid} ({shop_name}) — {len(files)} ảnh")

        # ── Tìm card_id ────────────────────────────────────────────────────
        # Ưu tiên: card_map (listing_id) > existing_cards (title)
        if lid in card_map:
            card_id = card_map[lid]
            skip(f"  Đã có trong card_map — skip tạo card")
            total_skipped += 1
        elif card_name in existing_cards:
            card_id = existing_cards[card_name]
            card_map[lid] = card_id
            skip(f"  Title đã tồn tại — dùng lại card (id={card_id})")
            total_skipped += 1
        else:
            # Tạo card mới
            card_id = create_card(TRELLO_LIST_ID, card_name, desc)
            card_map[lid] = card_id
            existing_cards[card_name] = card_id
            total_created += 1
            done(f"  Tạo card (id={card_id})")

        # ── Upload từng file ────────────────────────────────────────────────
        ok = fail = 0
        for j, file_path in enumerate(files, 1):
            filename = os.path.basename(file_path)
            size_kb  = os.path.getsize(file_path) // 1024
            info(f"  [{j}/{len(files)}] Upload {filename} ({size_kb}KB)...")

            if upload_file(card_id, file_path):
                ok += 1
                done(f"    OK")
            else:
                fail += 1

            time.sleep(DELAY_UPLOAD)

        total_uploaded += ok
        total_failed   += fail
        done(f"  → {ok}/{len(files)} upload OK"
             + (f" | {fail} thất bại" if fail else ""))

        save_card_map(card_map)
        time.sleep(DELAY_CARD)

    # Summary
    save_card_map(card_map)
    print(f"\n{'─'*55}")
    done("Upload hoàn tất!")
    info(f"  Cards tạo mới   : {total_created}")
    info(f"  Cards skipped   : {total_skipped}")
    info(f"  Files uploaded  : {total_uploaded}")
    info(f"  Files failed    : {total_failed}")
    info(f"  Card map saved  : {CARD_MAP_FILE}")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    run()
