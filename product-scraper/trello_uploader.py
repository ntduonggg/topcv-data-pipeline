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
TRELLO_API_KEY  = os.abort("TRELLO_API_KEY not set in environment") if "TRELLO_API_KEY" not in os.environ else os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN    = os.abort("TRELLO_TOKEN not set in environment") if "TRELLO_TOKEN" not in os.environ else os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.abort("TRELLO_BOARD_ID not set in environment") if "TRELLO_BOARD_ID" not in os.environ else os.getenv("TRELLO_BOARD_ID")

INPUT_CSV = "heyetsy_image_urls.csv"
#INPUT_CSV = "hidden_listings.csv"

DELAY_CARD        = 0.3
DELAY_ATTACHMENT  = 0.2
DELAY_LIST        = 1.0
DELAY_BETWEEN_LISTS = 10.0

TRELLO_BASE = "https://api.trello.com/1"

VN_OFFSET  = timedelta(hours=7)

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
def fetch_board_lists(board_id: str) -> List[Dict]:
    """Trả về list các {id, name} của tất cả lists trên board."""
    return _get(f"/boards/{board_id}/lists", {"fields": "id,name"})

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

def get_latest_card(board_id: str) -> str:
    """
    Trả về card mới nhất trên board.
    """
    lists = fetch_board_lists(board_id)
    latest_card = None
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id,name,dateLastActivity", "limit": 1000})
        latest_card = cards[-1] if cards else None
        # for card in cards:
        #     if not latest_card or card["dateLastActivity"] > latest_card["dateLastActivity"]:
        #         latest_card = card
    return latest_card["name"] if latest_card else None

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


def count_cards_per_list(board_id: str) -> List[Dict]:
    """
    Trả về list [{name, id, card_count}] cho mỗi list trên board.
    """
    lists = fetch_board_lists(board_id)
    result = []
    for lst in lists:
        cards = _get(f"/lists/{lst['id']}/cards", {"fields": "id"})
        result.append({"name": lst["name"], "id": lst["id"], "card_count": len(cards)})
    return result

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
    """Format: [NTD050626A01] — prefix + ddmmyy + letter + zero-padded counter."""
    date_str = datetime.now().strftime("%d%m%y")
    seq      = str(counter).zfill(2)   # 01→99 tự mở rộng khi ≥100
    return f"[{prefix}{date_str}{letter}{seq}]"

def get_card_counter_start(board_id: str) -> int:
    """
    Lấy counter tiếp theo dựa trên card cuối cùng trên board.
    - Cùng ngày  → tiếp tục số tiếp theo (VD: 256 → 257)
    - Khác ngày  → reset về 1
    - Board rỗng / không parse được → bắt đầu từ 1
    """
    today = datetime.now().strftime("%d%m%y")
    info("Tính card counter bắt đầu dựa trên card cuối cùng trên board...")
    try:
        last_name = get_latest_card(board_id)
        if not last_name:
            return 1

        # lists = _get(f"/boards/{board_id}/lists", {"fields": "id"})
        # for lst in reversed(lists):
        #     cards = _get(f"/lists/{lst['id']}/cards", {"fields": "name"})
        #     if not cards:
        #         continue  # VD: NTD050626A256
        m = re.match(r"\[[A-Z]+(\d{6})[A-Z](\d+)\]", last_name)
        card_date    = m.group(1)        # 050626
        card_counter = int(m.group(2))   # 256
        if card_date == today:
            return card_counter + 1      # tiếp tục: 257
        else:
            return 1                     # ngày mới: reset
    except Exception:
        pass
    return 1   # fallback

def find_title_in_name(name: str) -> str:
    """Lấy title từ card name (format: [NTD120626A01] Title...)."""
    m = re.match(r"^\[.*?\]\s*(.+)", name or "", re.DOTALL)
    return m.group(1).strip() if m else ""

def build_description(row: pd.Series) -> str:
    parts = []
    if row.get("tags"):
        parts.append(f"{row['tags']}")
    return "\n\n".join(parts).strip()

# ── Parse helpers ─────────────────────────────────────────────────────────────
def build_new_desc(tags: Optional[str]) -> str:
    """Chỉ còn tags, không có prefix 'Tags:', không có Title, không có Etsy URL."""
    return tags.strip() if tags else ""

def name_is_correct(name: str) -> bool:
    """Card name đúng dạng [SKU] Title."""
    return bool(re.match(r"^\[[A-Z]+\d{6}[A-Z]\d+\] .+", name))

def desc_is_correct(desc: str) -> bool:
    """
    Description đúng nếu:
    - Không chứa **Title:**
    - Không chứa **Tags:**
    - Không chứa **Etsy URL:**
    """
    return (
        "**Title:**" not in (desc or "")
        and "**Tags:**" not in (desc or "")
        and "**Etsy URL:**" not in (desc or "")
    )

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
    total_listings = len(df)

    # ── B1: Đếm cards theo list ───────────────────────────────────────────────
    info("Kiểm tra trạng thái board...")
    list_stats   = count_cards_per_list(board_id)
    total_cards  = sum(s["card_count"] for s in list_stats)
    shops        = df["shop_name"].unique().tolist()
    
    sep()
    info(f"Board hiện có {len(list_stats)} List(s) active.")
    info(f"Card mới nhất trên board: {get_latest_card(board_id)}")

    if total_cards == 0:
        info("Board rỗng — bắt đầu upload mới.")
        sep()
        start_idx = _confirm_and_get_start(0, total_listings)
        return start_idx

    sep()

    # Hiển thị breakdown + tính suggested dựa theo thứ tự CSV
    info(f"Tìm thấy {total_cards} card (tổng {total_listings} listings), trong đó:")

    # Map list_name → card_count từ Trello
    trello_card_count = {s["name"]: s["card_count"] for s in list_stats}

    # Tính cumulative theo đúng thứ tự shop trong CSV
    running = 0
    resume_suggestions = []  # [(shop_name, card_idx_in_shop, global_row)]

    for shop_name in shops:
        shop_total      = len(df[df["shop_name"] == shop_name])
        cards_in_trello = trello_card_count.get(shop_name, 0)
        print(f"  * {cards_in_trello} card trong List {shop_name} (tổng {shop_total} listings)")

        #if cards_in_trello > 0:
            # Dòng global để resume list này = running + cards_in_trello + 1
        resume_row = running + cards_in_trello + 1  # 1-based
        resume_suggestions.append((shop_name, cards_in_trello + 1, resume_row))

        running += shop_total

    sep()

    # ── B2: Resume hay Reset ──────────────────────────────────────────────────
    ans = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục upload(Y)/Reset(n): "
    ).strip().lower()

    if ans == "n":
        # Reset: xác nhận xoá
        confirm = input(
            f"{ts()} {C.tag(C.ERROR, 'CONFIRM')} "
            f"Xoá toàn bộ {total_cards} cards + {len(list_stats)} lists? (yes/no): "
        ).strip().lower()

        if confirm != "yes":
            info("Huỷ reset — thoát.")
            sys.exit(0)

        info(f"Đang xoá {total_cards} cards + archive lists...")
        deleted = delete_all_board_cards_and_lists(board_id)
        done(f"Đã xoá {deleted} cards.")
        start_idx = 0

    else:
        # Resume: dùng suggested hoặc cho nhập tay
        # Tính suggested = dòng tiếp theo sau card cuối cùng đã upload
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

        # Hiển thị gợi ý resume từng list
        print()
        info("Gợi ý resume:")
        for shop_name, card_in_shop, global_row in resume_suggestions:
            cards_done = card_in_shop - 1
            print(f"  * resume list {shop_name} tại card {cards_done + 1} [nhập {global_row + 1}]")


        custom = input(
            f"{ts()} {C.tag(C.INFO, 'INFO')}  "
            f"Nhập dòng bắt đầu [mặc định {suggested + 2}]: "
        ).strip()

        if custom.isdigit():
            start_idx = int(custom) - 1  # convert sang 0-based
        else:
            start_idx = suggested

        info(f"Sẽ bắt đầu từ dòng {start_idx + 1}/{total_listings}")

    sep()

    return start_idx - 1

def _confirm_and_get_start(suggested: int, total: int) -> int:
    """Dùng cho trường hợp board rỗng — vẫn cho phép chọn dòng bắt đầu."""
    custom = input(
        f"{ts()} {C.tag(C.INFO, 'INFO')}  "
        f"Nhập dòng bắt đầu [mặc định 1]: "
    ).strip()
    start_idx = int(custom) - 1 if custom.isdigit() else 0

    final = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục upload từ dòng {start_idx + 2}/{total}? (Y/n): "
    ).strip().lower()

    if final == "n":
        info("Huỷ — thoát.")
        sys.exit(0)

    return start_idx


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

    # ── B1 → B3: Startup check ────────────────────────────────────────────────
    start_idx = startup_check(board_id, df)

    # ── B3: Xác nhận lần cuối ────────────────────────────────────────────────
    final = input(
        f"{ts()} {C.tag(C.WARN, 'WARN')}  "
        f"Tiếp tục upload từ dòng {start_idx + 2}? (Y/n): "
    ).strip().lower()

    if final == "n":
        info("Huỷ — thoát.")
        sys.exit(0)

    existing_lists    = fetch_existing_lists(board_id)
    existing_cards_cache: Dict[str, Dict] = {}
    shops             = df["shop_name"].unique().tolist()
    df_from           = df.iloc[start_idx:].copy()
    card_counter = get_card_counter_start(board_id)

    info(f"Tổng {len(df)} listings | Bắt đầu từ dòng {start_idx + 2}\n")

    df_from = df.iloc[start_idx + 1:].copy()
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

         # Tính global row index của listing đầu tiên trong shop này
        first_global_idx = start_idx + 2
        last_global_idx  = start_idx + len(shop_rows) + 1

        info(f"[{shop_idx}/{len(shops)}] List: {shop_name}  "
             f"({last_global_idx - first_global_idx + 1} listings remaining | dòng {first_global_idx}–{last_global_idx})")
        
        time.sleep(1)  # nghỉ 1s trước khi xử lý list mới

        list_id = get_or_create_list(board_id, shop_name, existing_lists)

        # Fetch cards + attachment count 1 lần / list
        if list_id not in existing_cards_cache:
            existing_cards_cache[list_id] = fetch_cards_with_attachments(list_id)
        existing_cards = existing_cards_cache[list_id]


        for row_idx, (_, row) in enumerate(shop_rows.iterrows(), 1):
            if _interrupted:
                break

            title      = row.get("title") or row.get("listing_id", f"listing_{row_idx}")
            card_name  = make_card_name(card_counter) + " " + title  # prefix + title (cắt bớt nếu dài)
            desc       = build_description(row)
            image_urls = extract_images(row)

            # So sánh theo title trong card_name
            matched_card = next(
                (info_dict for existing_name, info_dict in existing_cards.items()
                 if find_title_in_name(existing_name) == title
                 or existing_name == title),  # fallback nếu card cũ chưa có title trong desc
                None
            )

            if matched_card:
                card_id   = matched_card["id"]
                att_count = matched_card["attachment_count"]
                desc_old  = matched_card["desc"]
                desc_ok = desc_is_correct(desc_old) and desc_old == desc

                if att_count >= len(image_urls):
                    # Card đã có đủ attachment → skip
                    skip(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:60]}' "
                         f"— đã có {att_count} att, skip")
                    cards_skipped += 1
                else:
                    # Card có nhưng thiếu attachment → re-attach
                    delete_all_attachments(card_id)
                    attached = 0
                    for url in image_urls:
                        if attach_url(card_id, url):
                            attached += 1
                        time.sleep(DELAY_ATTACHMENT)
                    attachments_total += attached
                    cards_reattach += 1
                    card_counter += 1
                    upd(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:60]}' "
                        f"— re-attach {attached}/{len(image_urls)} att")
                    
                if not desc_ok:
                    if update_card(card_id, card_name, desc):
                        upd(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:60]}' "
                        f"— update description SUCCESS")
                    else:
                        warn(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:60]}' "
                        f"— update description FAILED")

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
                done(f"  [{row_idx}/{len(shop_rows)}] '{card_name[:60]}' "
                     f"— {attached}/{len(image_urls)} attachments")

            time.sleep(DELAY_CARD)

        # ── Cuối mỗi list: hỏi có tiếp tục qua list tiếp theo ────────────────
        if _interrupted:
            break

        # Kiểm tra còn list tiếp theo không
        remaining_shops = [
            s for s in shops[shop_idx:]
            if not df_from[df_from["shop_name"] == s].empty
        ]
        if not remaining_shops:
            break  # hết list, không cần hỏi

        sep()
        info(f"Hoàn thành List '{shop_name}'.")
        info(f"Nghỉ {DELAY_BETWEEN_LISTS}s trước khi chuyển sang List '{remaining_shops[0]}'...")

        # Đếm ngược
        for remaining in range(DELAY_BETWEEN_LISTS, 0, -10):
            print(f"  {remaining}s...", end="\r")
            time.sleep(10)
        print()

        cont = input(
            f"{ts()} {C.tag(C.WARN, 'WARN')}  "
            f"Tiếp tục qua List '{remaining_shops[0]}'? (Y/n): "
        ).strip().lower()

        if cont == "n":
            stop("Dừng theo yêu cầu.")
            break

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    sep()
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