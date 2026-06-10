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
TRELLO_API_KEY  = os.abort("TRELLO_API_KEY not set in environment") if "TRELLO_API_KEY" not in os.environ else os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN    = os.abort("TRELLO_TOKEN not set in environment") if "TRELLO_TOKEN" not in os.environ else os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.abort("TRELLO_BOARD_ID not set in environment") if "TRELLO_BOARD_ID" not in os.environ else os.getenv("TRELLO_BOARD_ID")
#TRELLO_BOARD_ID = ""

INPUT_CSV = "heyetsy_image_urls.csv"

DELAY_CARD        = 0.3
DELAY_ATTACHMENT  = 0.2
DELAY_LIST        = 1.0

TRELLO_BASE = "https://api.trello.com/1"


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
    # Trello API nhận params qua query string, không phải request body
    resp = requests.post(
        f"{TRELLO_BASE}{endpoint}",
        params={**_auth(), **payload},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def _put(endpoint: str, payload: Dict = {}) -> dict:
    # Trello API nhận params qua query string
    resp = requests.put(
        f"{TRELLO_BASE}{endpoint}",
        params={**_auth(), **payload},
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

def delete_list_cards(list_id: str) -> int:
    """Xoá toàn bộ cards trong 1 list. Trả về số card đã xoá."""
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id"})
    count = 0
    for card in cards:
        _delete(f"/cards/{card['id']}")
        count += 1
        time.sleep(0.1)
    return count


# ── Card helpers ──────────────────────────────────────────────────────────────
def fetch_cards_with_attachments(list_id: str) -> Dict[str, Dict]:
    """
    Fetch toàn bộ cards + attachment count trong 1 API call.
    Trả về {card_name → {id, attachment_count}}.
    """
    cards = _get(
        f"/lists/{list_id}/cards",
        {"fields": "id,name,desc", "attachments": "true", "attachment_fields": "id"}
    )
    result = {}
    for card in cards:
        result[card["name"]] = {
            "id":               card["id"],
            "attachment_count": len(card.get("attachments", [])),
            "desc":             card.get("desc", ""),
        }
    return result

def create_card(list_id: str, name: str, desc: str = "") -> str:
    result = _post("/cards", {
        "idList": list_id,
        "name":   name,
        "desc":   desc,
        "pos":    "bottom",
    })
    return result["id"]

def update_card(card_id: str, name: str, desc: str) -> bool:
    try:
        _put(f"/cards/{card_id}", {"name": name, "desc": desc})
        return True
    except requests.HTTPError as e:
        warn(f"  Update card thất bại: {e}")
        return False

def count_board_cards(board_id: str) -> int:
    """Đếm tổng số cards trên board (qua tất cả lists)."""
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id"})
    total = 0
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id"})
        total += len(cards)
    return total

def delete_all_board_cards_and_lists(board_id: str) -> int:
    """Xoá toàn bộ cards + lists trên board. Trả về số cards đã xoá."""
    lists   = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    deleted = 0
    for lst in lists:
        n = delete_list_cards(lst["id"])
        deleted += n
        # Archive list (Trello không cho xoá list hoàn toàn qua API, chỉ archive)
        _put(f"/lists/{lst['id']}/closed", {"value": True})
        info(f"  Archived list '{lst['id']}' ({n} cards)")
    return deleted


# ── Attachment helpers ────────────────────────────────────────────────────────
def fetch_card_attachments(card_id: str) -> List[Dict]:
    try:
        return _get(f"/cards/{card_id}/attachments", {"fields": "id,url"})
    except Exception:
        return []

def delete_attachment(card_id: str, attachment_id: str) -> bool:
    return _delete(f"/cards/{card_id}/attachments/{attachment_id}")

def delete_all_attachments(card_id: str) -> int:
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

def extract_images(row: pd.Series) -> List[str]:
    return [
        row[c] for c in sorted(
            [c for c in row.index if re.match(r"^image_\d+$", c)],
            key=lambda x: int(x.split("_")[1])
        ) if row[c]
    ]

def make_card_name(counter: int, prefix: str = "NTD", letter: str = "A") -> str:
    """Format: NTD050626A01 — prefix + ddmmyy + letter + zero-padded counter."""
    date_str = datetime.now().strftime("%d%m%y")
    seq      = str(counter).zfill(2)   # 01→99 tự mở rộng khi ≥100
    return f"{prefix}{date_str}{letter}{seq}"

def get_card_counter_start(board_id: str) -> int:
    """
    Lấy counter tiếp theo dựa trên card cuối cùng trên board.
    - Cùng ngày  → tiếp tục số tiếp theo (VD: 256 → 257)
    - Khác ngày  → reset về 1
    - Board rỗng / không parse được → bắt đầu từ 1
    """
    today = datetime.now().strftime("%d%m%y")
    try:
        lists = _get(f"/boards/{board_id}/lists", {"fields": "id"})
        for lst in reversed(lists):
            cards = _get(f"/lists/{lst['id']}/cards", {"fields": "name"})
            if not cards:
                continue
            last_name = cards[-1]["name"]   # VD: NTD050626A256
            m = re.match(r"[A-Z]+(\d{6})[A-Z](\d+)$", last_name)
            if not m:
                break
            card_date    = m.group(1)        # 050626
            card_counter = int(m.group(2))   # 256
            if card_date == today:
                return card_counter + 1      # tiếp tục: 257
            else:
                return 1                     # ngày mới: reset
    except Exception:
        pass
    return 1   # fallback

def find_title_in_desc(desc: str) -> str:
    """Lấy title từ dòng đầu description (format: **Title:** ...)."""
    m = re.match(r"\*\*Title:\*\*\s*(.+)", desc or "")
    return m.group(1).strip() if m else ""

def build_description(row: pd.Series) -> str:
    parts = []
    title = row.get("title") or row.get("listing_id", "")
    if title:
        parts.append(f"**Title:** {title}")
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


# ── Startup flow ──────────────────────────────────────────────────────────────
def startup_check(board_id: str, df: pd.DataFrame) -> int:
    """
    Kiểm tra trạng thái board khi khởi động.

    - Board rỗng (0 cards)  → upload luôn, trả về resume_from=0
    - Board có X cards      → hỏi Resume/Reset:
        Y → tìm listing_id cuối cùng đã upload → trả về index để skip
        n → xác nhận xoá → xoá toàn bộ cards+lists → trả về 0

    Trả về index dòng trong df để bắt đầu upload (0 = từ đầu).
    """
    info("Kiểm tra trạng thái board...")
    total_cards = count_board_cards(board_id)

    if total_cards == 0:
        info("Board rỗng — bắt đầu upload mới.")
        return 0

    total_listings = len(df)
    print()
    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tìm thấy {total_cards} cards trên Trello (dòng thứ {total_cards + 1} trong file) (tổng {total_listings} listings). "
        f"Resume? (Y/n): "
    ).strip().lower()

    if ans != "n":
        # Resume: tìm listing_id cuối cùng đã có trên Trello
        resume_idx = find_resume_index(board_id, df)
        info(f"Resume từ dòng {resume_idx + 3}/{total_listings} "
             f"(listing_id={df.at[resume_idx, 'listing_id'] if resume_idx < total_listings else 'END'})")
        
        return resume_idx  # bắt đầu từ dòng KẾ TIẾP

    # Reset: xác nhận thêm lần nữa
    confirm = input(
        f"{ts()} {C.tag(C.ERROR, 'CONFIRM')} "
        f"Xoá toàn bộ {total_cards} cards? (yes/no): "
    ).strip().lower()

    if confirm != "yes":
        info("Huỷ reset — thoát.")
        sys.exit(0)

    info(f"Xoá toàn bộ {total_cards} cards + lists trên board...")
    deleted = delete_all_board_cards_and_lists(board_id)
    done(f"Đã xoá {deleted} cards.")
    return 0


def find_resume_index(board_id: str, df: pd.DataFrame) -> int:
    """
    Tìm index của listing cuối cùng đã upload lên Trello.
    Fetch cards từ list cuối cùng trên board, so sánh title với df.
    Trả về index đó trong df (để skip đến dòng tiếp theo).
    """
    lists = _get(f"/boards/{board_id}/lists", {"fields": "id,name"})
    if not lists:
        return 0

    # Lấy list cuối cùng
    last_list = lists[-1]
    cards = _get(f"/lists/{last_list['id']}/cards", {"fields": "id,name"})
    if not cards:
        # List cuối rỗng → thử list trước
        if len(lists) > 1:
            cards = _get(f"/lists/{lists[-2]['id']}/cards", {"fields": "id,name"})

    if not cards:
        return 0

    last_card_name = cards[-1]["name"]

    # Tìm index trong df theo title
    matches = df[df["title"] == last_card_name].index
    if not matches.empty:
        return int(matches[-1])

    # Fallback: tìm gần đúng theo listing_id nếu title không khớp
    return max(0, len(df) - len(cards) - 1)


# ── Main ──────────────────────────────────────────────────────────────────────
def upload_to_trello(
    csv_path: str = INPUT_CSV,
    board_id: str = TRELLO_BOARD_ID,
) -> None:
    """
    Upload/update heyetsy_image_urls.csv lên Trello.

    Startup flow:
    - Board rỗng              → upload từ đầu
    - Board có cards, Resume  → skip đến listing tiếp theo chưa upload
    - Board có cards, Reset   → xác nhận → xoá hết → upload từ đầu

    Logic mỗi card:
    - Card chưa tồn tại       → tạo mới + attach ảnh
    - Card có nhưng 0 att     → re-attach ảnh
    - Card có + đủ att        → skip
    """
    if not TRELLO_API_KEY or not TRELLO_TOKEN or not board_id:
        err("Chưa điền TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID!")
        sys.exit(1)

    setup_signal()
    df = load_csv(csv_path)

    # ── Startup check ─────────────────────────────────────────────────────────
    start_idx = startup_check(board_id, df)

    # Fetch existing lists (sau khi startup_check vì có thể đã archive hết)
    info("Fetching Lists trên board...")
    existing_lists = fetch_existing_lists(board_id)
    info(f"  Board hiện có {len(existing_lists)} List(s) active.")

    existing_cards_cache: Dict[str, Dict] = {}
    shops = df["shop_name"].unique().tolist()

    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục từ dòng {start_idx + 3}? (Y/n): "
    ).strip().lower()

    if ans == "n":
        start_idx = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Nhập dòng bắt đầu (1-based index, mặc định {start_idx + 3}): ")
        start_idx = int(start_idx) - 3

    info(f"Tổng {len(df)} listings | {len(shops)} shops |" 
         f"Bắt đầu từ dòng {start_idx + 3}\n")

    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục? (Y/n): "
    ).strip().lower()

    if ans == "n":
        info("Huỷ upload — thoát.")
        sys.exit(0)

    df_from = df.iloc[start_idx + 1:].copy()
    card_counter    = get_card_counter_start(board_id)
    cards_created   = 0
    cards_reattach  = 0
    cards_skipped   = 0
    attachments_total = 0

    # Group theo shop nhưng chỉ xử lý các dòng từ start_idx
    for shop_idx, shop_name in enumerate(shops, 1):
        if _interrupted:
            break

        shop_rows = df_from[df_from["shop_name"] == shop_name]
        if shop_rows.empty:
            continue

        info(f"[{shop_idx}/{len(shops)}] {shop_name}  ({len(shop_rows)} listings)")

        list_id = get_or_create_list(board_id, shop_name, existing_lists)

        # Fetch cards + attachment count 1 lần / list
        if list_id not in existing_cards_cache:
            existing_cards_cache[list_id] = fetch_cards_with_attachments(list_id)
        existing_cards = existing_cards_cache[list_id]

        for row_idx, (_, row) in enumerate(shop_rows.iterrows(), 1):
            if _interrupted:
                break

            title      = row.get("title") or row.get("listing_id", f"listing_{row_idx}")
            card_name  = make_card_name(card_counter)
            desc       = build_description(row)
            image_urls = extract_images(row)

            # So sánh theo title trong description (không dùng card_name vì đổi mỗi ngày)
            matched_card = next(
                (info_dict for existing_name, info_dict in existing_cards.items()
                 if find_title_in_desc(info_dict.get("desc", "")) == title
                 or existing_name == title),  # fallback nếu card cũ chưa có title trong desc
                None
            )

            if matched_card:
                card_id   = matched_card["id"]
                att_count = matched_card["attachment_count"]

                if att_count > 0:
                    # Card đã có đủ attachment → skip
                    skip(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                         f"— đã có {att_count} att, skip")
                    cards_skipped += 1
                else:
                    # Card có nhưng thiếu attachment → re-attach
                    attached = 0
                    for url in image_urls:
                        if attach_url(card_id, url):
                            attached += 1
                        time.sleep(DELAY_ATTACHMENT)
                    attachments_total += attached
                    cards_reattach += 1
                    card_counter += 1
                    upd(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                        f"— re-attach {attached}/{len(image_urls)} att")

            else:
                # Card chưa tồn tại → tạo mới
                card_id = create_card(list_id, card_name, desc)
                existing_cards[card_name] = {"id": card_id, "attachment_count": 0, "desc": desc}
                cards_created += 1
                card_counter += 1

                attached = 0
                for url in image_urls:
                    if attach_url(card_id, url):
                        attached += 1
                    time.sleep(DELAY_ATTACHMENT)

                attachments_total += attached
                done(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                     f"— {attached}/{len(image_urls)} attachments")

            time.sleep(DELAY_CARD)

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("─" * 60)
    done("Upload hoàn tất!")
    info(f"  Cards tạo mới    : {cards_created}")
    info(f"  Cards re-attach  : {cards_reattach}")
    info(f"  Cards skipped    : {cards_skipped}")
    info(f"  Attachments tổng : {attachments_total}")
    if _interrupted:
        warn("  (Dừng sớm do Ctrl+C)")


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    upload_to_trello()