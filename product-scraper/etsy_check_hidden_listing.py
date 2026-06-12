# """
# check_hidden_listings.py
# ─────────────────────────
# Đọc file CSV review (có cột title, url, listing_id),
# lấy danh sách unique listing → check từng URL trên Etsy.

# - "Sorry, this item is unavailable." hoặc lỗi → unavailable → lưu vào hidden_listings.csv
# - Sản phẩm bình thường → bỏ qua

# Output: hidden_listings.csv (title, listing_id, url)
# """

# import os
# import sys
# import time
# import signal
# import requests
# import pandas as pd
# from bs4 import BeautifulSoup
# from datetime import datetime
# from typing import Dict, List, Optional

# # ── ANSI color logging ────────────────────────────────────────────────────────
# class C:
#     INFO  = "\033[94m"
#     WARN  = "\033[93m"
#     CKPT  = "\033[92m"
#     ERROR = "\033[91m"
#     TIME  = "\033[96m"
#     DONE  = "\033[92m"
#     SKIP  = "\033[90m"
#     INTERRUPT = "\033[95m"
#     RESET = "\033[0m"

#     @staticmethod
#     def tag(color, label):
#         return f"{color}[{label}]{C.RESET}"

# def ts():
#     return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

# def info(msg):  print(f"{ts()} {C.tag(C.INFO,  'INFO')}  {msg}")
# def warn(msg):  print(f"{ts()} {C.tag(C.WARN,  'WARN')}  {msg}")
# def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,  'CKPT')}  {msg}")
# def err(msg):   print(f"{ts()} {C.tag(C.ERROR, 'ERROR')} {msg}")
# def done(msg):  print(f"{ts()} {C.tag(C.DONE,  'DONE')}  {msg}")
# def skip(msg):  print(f"{ts()} {C.tag(C.SKIP,  'SKIP')}  {msg}")
# def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")
# def sep():      print("─" * 60)


# # ── Config ────────────────────────────────────────────────────────────────────
# INPUT_CSV    = "etsy_review.csv"
# OUTPUT_CSV   = "hidden_listings.csv"
# CHECKPOINT_CSV = "check_hidden_checkpoint.csv"

# REQUEST_TIMEOUT = 15
# DELAY_BETWEEN   = 1.0   # giây giữa mỗi request

# # Các chuỗi xác nhận listing unavailable / không phải sản phẩm
# UNAVAILABLE_SIGNALS = [
#     "sorry, this item is unavailable",
#     "this item is unavailable",
#     "no longer available",
#     "this listing has expired",
#     "page not found",
#     "this shop is on vacation",
# ]

# USER_AGENT = (
#     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#     "AppleWebKit/537.36 (KHTML, like Gecko) "
#     "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
# )

# # Cookie từ session đã login (lấy từ DevTools, giống etsy_review_scraper.py)
# # Nếu để rỗng, Etsy có thể trả về trang chặn bot → mọi listing bị nhận nhầm là unavailable
# ETSY_COOKIE = ""

# # Lưu HTML debug khi nghi ngờ bị chặn (page không có cả unavailable signal lẫn h1)
# DEBUG_DIR = "debug_html"


# # ── CSV helpers ───────────────────────────────────────────────────────────────
# def load_csv(csv_path: str) -> pd.DataFrame:
#     for enc in ("utf-8-sig", "cp1252", "utf-8", "latin-1"):
#         try:
#             df = pd.read_csv(csv_path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
#             info(f"Loaded {len(df)} rows từ {csv_path} (encoding={enc})")
#             return df
#         except UnicodeDecodeError:
#             if enc == "latin-1":
#                 raise
#             continue
#     raise RuntimeError(f"Không đọc được {csv_path}")


# def extract_unique_listings(df: pd.DataFrame) -> List[Dict]:
#     """Lấy unique listing theo listing_id, giữ title + url."""
#     for col in ["title", "url", "listing_id"]:
#         if col not in df.columns:
#             raise ValueError(f"CSV thiếu cột '{col}'")

#     seen = {}
#     for _, row in df.iterrows():
#         lid = str(row.get("listing_id", "")).strip()
#         if not lid or lid in seen:
#             continue
#         seen[lid] = {
#             "listing_id": lid,
#             "title":      str(row.get("title", "")).strip(),
#             "url":        str(row.get("url", "")).strip(),
#         }

#     result = list(seen.values())
#     info(f"Unique listings: {len(result)}")
#     return result


# # ── Checkpoint ────────────────────────────────────────────────────────────────
# def load_checkpoint() -> set:
#     if not os.path.exists(CHECKPOINT_CSV):
#         return set()
#     try:
#         df = pd.read_csv(CHECKPOINT_CSV, dtype=str).fillna("")
#         return set(df["listing_id"].tolist())
#     except Exception:
#         return set()

# def save_checkpoint(done_ids: set) -> None:
#     pd.DataFrame({"listing_id": list(done_ids)}).to_csv(
#         CHECKPOINT_CSV, index=False, encoding="utf-8-sig"
#     )


# # ── Bot-check detection ────────────────────────────────────────────────────────
# BOT_CHECK_SIGNALS = [
#     "are you a robot",
#     "captcha",
#     "to discuss automated access",
#     "press and hold",
#     "verify you are a human",
# ]

# def is_bot_check_page(page_text: str) -> bool:
#     return any(sig in page_text for sig in BOT_CHECK_SIGNALS)


# # ── Check Etsy availability ───────────────────────────────────────────────────
# def check_etsy_unavailable(url: str, listing_id: str = "") -> Optional[bool]:
#     """
#     Truy cập URL Etsy listing.
#     Trả về:
#       True  → unavailable (text "Sorry, this item is unavailable" hoặc tương đương)
#       False → sản phẩm bình thường (có h1 title + KHÔNG có unavailable signal)
#       None  → lỗi request / bị chặn bot / không xác định được
#     """
#     headers = {
#         "User-Agent": USER_AGENT,
#         "Accept-Language": "en-US,en;q=0.9",
#     }
#     if ETSY_COOKIE:
#         headers["Cookie"] = ETSY_COOKIE

#     try:
#         resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
#     except Exception as e:
#         warn(f"  Request lỗi: {e}")
#         return None

#     if resp.status_code == 404:
#         return True

#     soup = BeautifulSoup(resp.text, "html.parser")
#     page_text = soup.get_text(" ", strip=True).lower()

#     # ── Check unavailable signal trước (dấu hiệu chắc chắn nhất) ──────────────
#     for sig in UNAVAILABLE_SIGNALS:
#         if sig in page_text:
#             return True

#     # ── Check bị chặn bot/captcha → None (không xác định) ─────────────────────
#     if is_bot_check_page(page_text):
#         warn(f"  Bị chặn bot/CAPTCHA — không xác định được")
#         if DEBUG_DIR and listing_id:
#             os.makedirs(DEBUG_DIR, exist_ok=True)
#             with open(os.path.join(DEBUG_DIR, f"{listing_id}_botcheck.html"), "w", encoding="utf-8") as f:
#                 f.write(resp.text)
#         return None

#     # ── Etsy redirect ra khỏi /listing/ → unavailable ─────────────────────────
#     if "/listing/" not in resp.url:
#         return True

#     # ── Có h1 title → sản phẩm bình thường ────────────────────────────────────
#     has_h1 = bool(soup.find("h1"))
#     if has_h1:
#         return False

#     # ── Không có unavailable signal, không bot-check, không h1 → nghi ngờ ─────
#     # Lưu HTML debug để kiểm tra thủ công, không kết luận unavailable
#     warn(f"  Không xác định được trạng thái (no h1, no signal) — lưu debug")
#     if DEBUG_DIR and listing_id:
#         os.makedirs(DEBUG_DIR, exist_ok=True)
#         with open(os.path.join(DEBUG_DIR, f"{listing_id}_unknown.html"), "w", encoding="utf-8") as f:
#             f.write(resp.text)
#     return None


# # ── Save output ───────────────────────────────────────────────────────────────
# def save_output(rows: List[Dict], path: str = OUTPUT_CSV) -> None:
#     df = pd.DataFrame(rows, columns=["title", "listing_id", "url"])
#     tmp = path + ".tmp"
#     df.to_csv(tmp, index=False, encoding="utf-8-sig")
#     os.replace(tmp, path)
#     ckpt(f"Saved → {path} ({len(df)} listings)")


# # ── Signal handler ────────────────────────────────────────────────────────────
# _interrupted = False
# _hidden_rows: List[Dict] = []
# _done_ids: set = set()

# def setup_signal():
#     def _handler(sig, frame):
#         global _interrupted
#         stop("Ctrl+C — lưu kết quả trước khi thoát...")
#         save_output(_hidden_rows)
#         save_checkpoint(_done_ids)
#         _interrupted = True
#         sys.exit(0)
#     signal.signal(signal.SIGINT, _handler)


# # ── Main ──────────────────────────────────────────────────────────────────────
# def run(input_csv: str = INPUT_CSV, output_csv: str = OUTPUT_CSV) -> pd.DataFrame:
#     global _hidden_rows, _done_ids

#     setup_signal()

#     df = load_csv(input_csv)
#     listings = extract_unique_listings(df)

#     # Resume checkpoint
#     _done_ids = load_checkpoint()
#     if _done_ids:
#         ans = input(
#             f"{ts()} {C.tag(C.CKPT, 'CKPT')} "
#             f"Tìm thấy checkpoint ({len(_done_ids)} đã check). Resume? (Y/n): "
#         ).strip().lower()
#         if ans == "n":
#             _done_ids = set()
#             if os.path.exists(CHECKPOINT_CSV):
#                 os.remove(CHECKPOINT_CSV)
#         else:
#             # Load lại hidden_rows đã có (nếu output cũ tồn tại)
#             if os.path.exists(output_csv):
#                 old = pd.read_csv(output_csv, dtype=str).fillna("")
#                 _hidden_rows = old.to_dict("records")
#                 info(f"Loaded {len(_hidden_rows)} hidden listings từ output cũ")

#     remaining = [l for l in listings if l["listing_id"] not in _done_ids]
#     info(f"Tổng {len(listings)} listings — còn lại {len(remaining)} cần check")

#     sep()

#     for i, item in enumerate(remaining, 1):
#         lid   = item["listing_id"]
#         title = item["title"]
#         url   = item["url"]

#         info(f"[{i}/{len(remaining)}] {lid} — '{title[:60]}'")

#         result = check_etsy_unavailable(url, listing_id=lid)

#         if result is None:
#             warn(f"  Lỗi request — không đánh dấu, sẽ check lại lần sau")
#             time.sleep(DELAY_BETWEEN)
#             continue  # không thêm vào done_ids → sẽ retry lần sau

#         if result is True:
#             done(f"  UNAVAILABLE")
#             _hidden_rows.append({"title": title, "listing_id": lid, "url": url})
#         else:
#             skip(f"  OK — sản phẩm bình thường")

#         _done_ids.add(lid)

#         # Lưu định kỳ mỗi 20 listing
#         if i % 20 == 0:
#             save_output(_hidden_rows)
#             save_checkpoint(_done_ids)

#         time.sleep(DELAY_BETWEEN)

#     # Final save
#     save_output(_hidden_rows)
#     save_checkpoint(_done_ids)

#     # Xoá checkpoint nếu hoàn tất toàn bộ
#     if len(_done_ids) >= len(listings):
#         if os.path.exists(CHECKPOINT_CSV):
#             os.remove(CHECKPOINT_CSV)
#             ckpt("Checkpoint đã xoá — hoàn tất toàn bộ.")

#     sep()
#     done("Hoàn tất!")
#     info(f"  Tổng listings checked : {len(_done_ids)}/{len(listings)}")
#     info(f"  Unavailable           : {len(_hidden_rows)}")
#     info(f"  Output                : {output_csv}")
#     sep()

#     return pd.DataFrame(_hidden_rows)


# # ── __main__ ──────────────────────────────────────────────────────────────────
# if __name__ == "__main__":
#     run()

import pandas as pd

INPUT_FILE = "etsy_review.csv"
OUTPUT_FILE = "hidden_listings.csv"
CUTOFF_DATE = "2026-03-03"
SHOP_NAME = "GONdesignJEWELRY"

df = pd.read_csv(INPUT_FILE, dtype=str)

df["_date"] = pd.to_datetime(df["review_date"], errors="coerce")
hidden = df[df["_date"] < CUTOFF_DATE].drop(columns=["_date"]).copy()

print(f"Tổng listing: {len(df)}")
print(f"Hidden (trước {CUTOFF_DATE}): {len(hidden)}")
print(f"Active (từ {CUTOFF_DATE} trở đi): {len(df) - len(hidden)}")

# Tìm tên cột thực tế (case-insensitive)
col_map = {c.lower(): c for c in hidden.columns}

hidden_out = pd.DataFrame({
    "shop_name":       SHOP_NAME,
    "listing_id":      hidden[col_map["listing_id"]],
    "title":           hidden[col_map["title"]],
    "tags":            "",
    "image_1":         hidden[col_map["image"]],
    "etsy_url":        hidden[col_map["url"]],
    "review_count":    hidden[col_map["review_count"]],
    "last_review_date":hidden[col_map["review_date"]],
})

hidden_out.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
print(f"Đã lưu: {OUTPUT_FILE}")