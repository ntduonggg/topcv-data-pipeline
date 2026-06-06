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
TRELLO_BOARD_ID = "6a222829ea1b5cc18b3d69ca"

INPUT_CSV         = "heyetsy_image_urls.csv"
DELAY_CARD        = 0.3
DELAY_ATTACHMENT  = 0.2
DELAY_BETWEEN_LISTS = 120   # giây nghỉ giữa các list

TRELLO_BASE = "https://api.trello.com/1"


# ── Trello API helpers ────────────────────────────────────────────────────────
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

def _put(endpoint: str, payload: Dict = {}) -> dict:
    resp = requests.put(f"{TRELLO_BASE}{endpoint}", params={**_auth(), **payload}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _delete(endpoint: str) -> bool:
    resp = requests.delete(f"{TRELLO_BASE}{endpoint}", params=_auth(), timeout=15)
    return resp.status_code == 200


# ── List helpers ──────────────────────────────────────────────────────────────
def fetch_board_lists(board_id: str) -> List[Dict]:
    """Trả về list các {id, name} của tất cả lists trên board."""
    return _get(f"/boards/{board_id}/lists", {"fields": "id,name"})

def fetch_existing_lists(board_id: str) -> Dict[str, str]:
    return {lst["name"]: lst["id"] for lst in fetch_board_lists(board_id)}

def create_list(board_id: str, name: str) -> str:
    return _post("/lists", {"name": name, "idBoard": board_id, "pos": "bottom"})["id"]

def get_or_create_list(board_id: str, name: str, existing: Dict[str, str]) -> str:
    if name in existing:
        return existing[name]
    list_id = create_list(board_id, name)
    existing[name] = list_id
    done(f"  Tạo List '{name}' (id={list_id})")
    return list_id

def delete_list_cards(list_id: str) -> int:
    cards = _get(f"/lists/{list_id}/cards", {"fields": "id"})
    for card in cards:
        _delete(f"/cards/{card['id']}")
        time.sleep(0.1)
    return len(cards)

def archive_list(list_id: str):
    _put(f"/lists/{list_id}/closed", {"value": "true"})

def delete_all_board_cards_and_lists(board_id: str) -> int:
    lists = fetch_board_lists(board_id)
    deleted = 0
    for lst in lists:
        n = delete_list_cards(lst["id"])
        deleted += n
        archive_list(lst["id"])
        info(f"  Archived '{lst['name']}' ({n} cards)")
    return deleted


# ── Card helpers ──────────────────────────────────────────────────────────────
def fetch_cards_with_attachments(list_id: str) -> Dict[str, Dict]:
    cards = _get(
        f"/lists/{list_id}/cards",
        {"fields": "id,name,desc", "attachments": "true", "attachment_fields": "id"}
    )
    return {
        c["name"]: {
            "id":               c["id"],
            "attachment_count": len(c.get("attachments", [])),
            "desc":             c.get("desc", ""),
        }
        for c in cards
    }

def create_card(list_id: str, name: str, desc: str = "") -> str:
    return _post("/cards", {"idList": list_id, "name": name, "desc": desc, "pos": "bottom"})["id"]

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

def find_title_in_desc(desc: str) -> str:
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


# ── Card naming ───────────────────────────────────────────────────────────────
def make_card_name(counter: int, prefix: str = "NTD", letter: str = "A") -> str:
    date_str = datetime.now().strftime("%d%m%y")
    return f"{prefix}{date_str}{letter}{str(counter).zfill(2)}"

def get_card_counter_start(board_id: str) -> int:
    today = datetime.now().strftime("%d%m%y")
    try:
        lists = _get(f"/boards/{board_id}/lists", {"fields": "id"})
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
    B1: Đếm cards trên board, hiển thị breakdown theo list.
    B2: Resume (Y) hoặc Reset (n).
    B3: Xác nhận lần cuối trước khi upload.
    Trả về start_idx (0-based) trong df.
    """
    total_listings = len(df)

    # ── B1: Đếm cards theo list ───────────────────────────────────────────────
    info("Đang đếm cards trên board...")
    list_stats   = count_cards_per_list(board_id)
    total_cards  = sum(s["card_count"] for s in list_stats)
    shops        = df["shop_name"].unique().tolist()

    sep()
    info(f"Board hiện có {len(list_stats)} List(s) active.")

    if total_cards == 0:
        info("Board rỗng — bắt đầu upload mới.")
        sep()
        start_idx = _confirm_and_get_start(0, total_listings)
        return start_idx

    # Hiển thị breakdown
    # info(f"Tìm thấy {total_cards} card (tổng {total_listings} listings), trong đó:")
    # cumulative = 0
    # for s in list_stats:
    #     # Tính tổng listings của shop này
    #     shop_total = len(df[df["shop_name"] == s["name"]]) if s["name"] in shops else 0
    #     print(f"  * {s['card_count']} card trong List {s['name']} (tổng {shop_total} listings)")
    #     cumulative += s["card_count"]

    # suggested = total_cards  # đề xuất mặc định: dòng tiếp theo sau card cuối
    # sep()

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
    card_counter      = get_card_counter_start(board_id)

    info(f"Tổng {len(df)} listings | Bắt đầu từ dòng {start_idx + 2}\n")

    cards_created     = 0
    cards_reattach    = 0
    cards_skipped     = 0
    attachments_total = 0

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
        
        time.sleep(30)  # nghỉ 1s trước khi xử lý list mới

        list_id = get_or_create_list(board_id, shop_name, existing_lists)

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

            matched_card = next(
                (d for n, d in existing_cards.items()
                 if find_title_in_desc(d.get("desc", "")) == title or n == title),
                None
            )

            if matched_card:
                card_id   = matched_card["id"]
                att_count = matched_card["attachment_count"]
                if att_count > 0:
                    skip(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                         f"— {att_count} att, skip")
                    cards_skipped += 1
                else:
                    attached = sum(
                        1 for url in image_urls
                        if attach_url(card_id, url) or not time.sleep(DELAY_ATTACHMENT)
                    )
                    attachments_total += attached
                    cards_reattach += 1
                    card_counter   += 1
                    upd(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                        f"— re-attach {attached}/{len(image_urls)}")
            else:
                card_id = create_card(list_id, card_name, desc)
                existing_cards[card_name] = {"id": card_id, "attachment_count": 0, "desc": desc}
                cards_created += 1
                card_counter  += 1

                attached = sum(
                    1 for url in image_urls
                    if attach_url(card_id, url) or not time.sleep(DELAY_ATTACHMENT)
                )
                attachments_total += attached
                done(f"  [{row_idx}/{len(shop_rows)}] '{card_name}' '{title[:40]}' "
                     f"— {attached}/{len(image_urls)} att")

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