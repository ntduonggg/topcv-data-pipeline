"""
heyetsy_tags_refill.py
────────────────────────
Đọc heyetsy_image_urls.csv → lọc dòng tags rỗng
→ crawl https://heyetsy.com/listing/{listing_id} lấy tags
→ ghi đè vào đúng dòng trong heyetsy_image_urls.csv (dạng "tag1, tag2, tag3, ...")

Giống cơ chế heyetsy_image_refill.py: ghi đúng dòng, lưu định kỳ, Ctrl+C safe.
"""

import time
import re
import random
import sys
import os
import signal
from datetime import datetime
from typing import List

import pandas as pd
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO   = "\033[94m"
    ACTION = "\033[94m"
    WARN   = "\033[93m"
    CKPT   = "\033[92m"
    ERROR  = "\033[91m"
    INTERRUPT = "\033[95m"
    TIME   = "\033[96m"
    DONE   = "\033[92m"
    RESET  = "\033[0m"

    @staticmethod
    def tag(color, label):
        return f"{color}[{label}]{C.RESET}"

def ts():
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,      'INFO')}  {msg}")
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,      'WARN')}  {msg}")
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,      'CKPT')}  {msg}")
def err(msg):   print(f"{ts()} {C.tag(C.ERROR,     'ERROR')} {msg}")
def done(msg):  print(f"{ts()} {C.tag(C.DONE,      'DONE')}  {msg}")
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV      = "heyetsy_image_urls.csv"   # ghi đè trực tiếp file này
HEYETSY_BASE   = "https://heyetsy.com/listing"
PAGE_LOAD_WAIT = 10
DELAY_MIN      = 3.0
DELAY_MAX      = 4.5
SAVE_EVERY     = 10

RATE_LIMIT_SIGNALS = [
    "429", "Too Many Requests", "too many requests",
    "rate limit", "Rate Limit", "slow down", "Slow Down",
]


# ── build_driver ──────────────────────────────────────────────────────────────
def build_driver(headless: bool = False) -> webdriver.Edge:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    )
    return webdriver.Edge(options=options)


# ── wait_for_login ────────────────────────────────────────────────────────────
def wait_for_login(driver: webdriver.Edge, timeout: int = 180) -> bool:
    driver.get("https://heyetsy.com/login")
    print(f"\n{ts()} {C.tag(C.ACTION, 'ACTION')} Browser đã mở trang login HeyEtsy.")
    print(f"{ts()} {C.tag(C.ACTION, 'ACTION')} Vui lòng đăng nhập vào tài khoản HeyEtsy.")
    info(f"Chờ login tối đa {timeout}s...")
    for _ in range(timeout):
        time.sleep(1)
        if "/login" not in driver.current_url and "heyetsy.com" in driver.current_url:
            done("Login thành công!")
            return True
    err("Timeout — không phát hiện login.")
    return False


# ── smart_sleep ───────────────────────────────────────────────────────────────
def smart_sleep(min_s: float = DELAY_MIN, max_s: float = DELAY_MAX):
    time.sleep(random.uniform(min_s, max_s))

def is_rate_limited(page_source: str) -> bool:
    return any(s in page_source for s in RATE_LIMIT_SIGNALS)


# ── parse_tags ────────────────────────────────────────────────────────────────
def parse_tags(page_source: str) -> List[str]:
    """
    Parse tags từ trang heyetsy listing.

    Cấu trúc HTML thực tế:
      <ul role="list" class="mt-2 space-x-1 space-y-1 leading-8">
        <li class="inline-flex" x-data="{ copied: false }">
          <span class="inline-flex relative items-center ... rounded-full border border-gray-300">
            <div class="flex absolute ...">...</div>
            <div class="ml-3.5 text-xs font-semibold text-gray-900 lowercase">Pottery Shirt</div>
          </span>
          <button @click="...copy...">...</button>
        </li>
        ...
      </ul>
    """
    soup = BeautifulSoup(page_source, "lxml")
    tags: List[str] = []
    seen = set()

    # Mỗi tag = <div class="ml-3.5 ... lowercase"> bên trong <li x-data="{ copied: false }">
    for li in soup.select('ul[role="list"] li[x-data*="copied"]'):
        el = li.select_one('div[class*="ml-3"]')
        if not el:
            # fallback: lấy div cuối cùng trong <span>
            span = li.find("span")
            if span:
                divs = span.find_all("div")
                el = divs[-1] if divs else None
        if not el:
            continue

        text = el.get_text(strip=True)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            tags.append(text)

    # Fallback nếu cấu trúc thay đổi: tìm bất kỳ li[x-data*="copied"] → text trực tiếp
    if not tags:
        for li in soup.select('li[x-data*="copied"]'):
            text = li.get_text(strip=True)
            if text and len(text) <= 50 and text.lower() not in seen:
                seen.add(text.lower())
                tags.append(text)

    return tags


# ── scrape_one ────────────────────────────────────────────────────────────────
def scrape_one(driver: webdriver.Edge, listing_id: str) -> List[str]:
    """Crawl 1 listing, không retry inline. 429 → trả về []."""
    url = f"{HEYETSY_BASE}/{listing_id}"

    def _load():
        try:
            driver.get(url)
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            smart_sleep(1.5, 2.5)
        except Exception:
            smart_sleep(2.0, 2.5)

    _load()

    if "/login" in driver.current_url:
        err(f"Session hết hạn tại listing {listing_id} — re-login...")
        wait_for_login(driver)
        _load()

    page_src = driver.page_source

    # if is_rate_limited(page_src):
    #     warn(f"429 detected tại listing {listing_id} — bỏ qua lần này.")
    #     return []

    tags = parse_tags(page_src)

    # if not tags:
    #     debug_path = f"debug_tags_{listing_id}.html"
    #     with open(debug_path, "w", encoding="utf-8") as f:
    #         f.write(page_src)
    #     warn(f"Không parse được tags — HTML đã lưu → {debug_path}")

    return tags


# ── save_df ───────────────────────────────────────────────────────────────────
def save_df(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Saved → {path}")


# ── fill_missing_tags ─────────────────────────────────────────────────────────
def fill_missing_tags(csv_path: str = INPUT_CSV, headless: bool = False) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        err(f"Không tìm thấy file: {csv_path}")
        sys.exit(1)

    df = pd.read_csv(csv_path, dtype=str).fillna("")
    info(f"Loaded {len(df)} rows từ {csv_path}")

    if "listing_id" not in df.columns:
        err("CSV thiếu cột 'listing_id'")
        sys.exit(1)
    if "tags" not in df.columns:
        df["tags"] = ""

    missing_mask = df["tags"] == ""
    missing_df   = df[missing_mask]
    info(f"Dòng tags rỗng: {len(missing_df)} / {len(df)}")

    if missing_df.empty:
        done("Không có dòng nào thiếu tags — không cần làm gì.")
        return df

    def _handler(sig, frame):
        stop("Ctrl+C — lưu CSV trước khi thoát...")
        save_df(df, csv_path)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)

    driver = build_driver(headless=headless)
    filled_count = still_missing = 0
    items_since_save = 0

    try:
        if not wait_for_login(driver):
            err("Login thất bại — thoát.")
            driver.quit()
            sys.exit(1)

        total = len(missing_df)
        for i, (idx, row) in enumerate(missing_df.iterrows(), 1):
            lid       = row.get("listing_id", "")
            shop_name = row.get("shop_name", "")

            if not lid:
                warn(f"  [{i}/{total}] Dòng {idx} không có listing_id — bỏ qua.")
                continue

            info(f"[{i}/{total}] listing={lid}  shop={shop_name}")

            tags = scrape_one(driver, lid)

            if tags:
                tags_str = ", ".join(tags)
                done(f"  {len(tags)} tags → ghi vào dòng {idx}: {tags_str[:80]}{'...' if len(tags_str) > 80 else ''}")
                df.at[idx, "tags"] = tags_str
                filled_count += 1
                items_since_save += 1
            else:
                warn(f"  Vẫn không có tags — giữ nguyên dòng {idx}")
                still_missing += 1

            if items_since_save >= SAVE_EVERY:
                save_df(df, csv_path)
                items_since_save = 0

            smart_sleep(DELAY_MIN, DELAY_MAX)

    finally:
        driver.quit()
        info("Browser đã đóng.")

    save_df(df, csv_path)

    print(f"\n{'─'*50}")
    done("Hoàn tất bổ sung tags!")
    info(f"  Đã bổ sung thành công : {filled_count}")
    info(f"  Vẫn còn thiếu tags    : {still_missing}")
    info(f"  File output            : {csv_path}")
    print(f"{'─'*50}\n")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fill_missing_tags(csv_path=INPUT_CSV, headless=False)