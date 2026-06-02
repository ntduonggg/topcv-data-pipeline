import time
import re
import random
import sys
import os
import signal
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup

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
    RETRY     = "\033[95m"
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
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}")
def retry(msg): print(f"{ts()} {C.tag(C.RETRY,     'RETRY')} {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
HEYETSY_BASE     = "https://heyetsy.com/listing"
CHECKPOINT_CSV   = "heyetsy_checkpoint.csv"
OUTPUT_CSV       = "heyetsy_image_urls.csv"
CHECKPOINT_EVERY = 20

PAGE_LOAD_WAIT = 10    # giây chờ JS render (tăng từ 6 → 10)

# ── Delay config (P1) ─────────────────────────────────────────────────────────
BASE_DELAY_MIN  = 2.0   # delay tối thiểu giữa các listing (tăng từ 0.5)
BASE_DELAY_MAX  = 3.5   # delay tối đa giữa các listing

# ── Retry config (P1 + P2) ───────────────────────────────────────────────────
MAX_RETRY       = 3     # số lần thử lại khi không có ảnh hoặc gặp 429
BACKOFF_BASE    = 30    # giây nghỉ cơ bản khi retry (x attempt → exponential)

# ── Periodic break config (P3) ───────────────────────────────────────────────
BREAK_EVERY     = 50    # nghỉ dài sau mỗi N listings
BREAK_DURATION  = 120   # giây nghỉ (2 phút)

# ── 429 detection keywords ────────────────────────────────────────────────────
RATE_LIMIT_SIGNALS = [
    "429",
    "Too Many Requests",
    "too many requests",
    "rate limit",
    "Rate Limit",
    "slow down",
    "Slow Down",
]


# ── build_driver ──────────────────────────────────────────────────────────────
def build_driver(headless: bool = False) -> webdriver.Edge:
    """HeyEtsy render bằng Livewire/JS → cần Selenium chờ DOM."""
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


# ── wait_for_login ───────────────────────────────────────────────────────────
def wait_for_login(driver: webdriver.Edge, timeout: int = 180) -> bool:
    """
    Mở trang login HeyEtsy → chờ user login thủ công.
    Phát hiện login thành công khi URL không còn chứa '/login'.
    """
    driver.get("https://heyetsy.com/login")
    print(f"\n{ts()} {C.tag(C.ACTION, 'ACTION')} Browser đã mở trang login HeyEtsy.")
    print(f"{ts()} {C.tag(C.ACTION, 'ACTION')} Vui lòng đăng nhập vào tài khoản HeyEtsy.")
    info(f"Chờ login tối đa {timeout}s...")

    elapsed = 0
    while elapsed < timeout:
        time.sleep(1)
        elapsed += 1
        current_url = driver.current_url
        if "/login" not in current_url and "heyetsy.com" in current_url:
            done("Login thành công!")
            return True

    err(f"Timeout {timeout}s — không phát hiện login.")
    return False


# ── smart_sleep ───────────────────────────────────────────────────────────────
def smart_sleep(min_s: float = BASE_DELAY_MIN, max_s: float = BASE_DELAY_MAX):
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


# ── is_rate_limited ───────────────────────────────────────────────────────────
def is_rate_limited(page_source: str) -> bool:
    """P2: Phát hiện trang trả về 429 / Too Many Requests."""
    for signal_str in RATE_LIMIT_SIGNALS:
        if signal_str in page_source:
            return True
    return False


# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_flush(log: List[Dict], path: str = CHECKPOINT_CSV) -> None:
    if not log:
        return
    tmp = path + ".tmp"
    pd.DataFrame(log).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Flush {len(log)} records → {path}")

def checkpoint_load(path: str = CHECKPOINT_CSV) -> Tuple[List[Dict], List[str]]:
    """Trả về (log, done_ids)."""
    if not os.path.exists(path):
        return [], []
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        log      = df.to_dict("records")
        done_ids = df["listing_id"].tolist() if "listing_id" in df.columns else []
        ckpt(f"Loaded {len(done_ids)} listing IDs từ checkpoint.")
        return log, done_ids
    except Exception as e:
        warn(f"Không đọc được checkpoint: {e}")
        return [], []

def checkpoint_setup_signal(log: List[Dict]) -> None:
    def _handler(sig, frame):
        stop("Ctrl+C — flush checkpoint rồi thoát...")
        checkpoint_flush(log)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── parse_image_urls ──────────────────────────────────────────────────────────
def parse_image_urls(page_source: str) -> List[str]:
    """
    Snapshot page_source → BS4 parse.
    Lấy href từ <a href="https://i.etsystatic.com/...il_fullxfull....jpg"
                    download="il_fullxfull....jpg">
    """
    soup = BeautifulSoup(page_source, "lxml")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if (
            "i.etsystatic.com" in href
            and "il_fullxfull" in href
            and href not in seen
        ):
            urls.append(href)
            seen.add(href)
    return urls


# ── scrape_listing_images ─────────────────────────────────────────────────────
def scrape_listing_images(
    driver: webdriver.Edge,
    listing_id: str,
) -> List[str]:
    """
    Mở heyetsy.com/listing/{id}, chờ JS render,
    trả về list image URLs (fullxfull, không download).

    Tích hợp P1 + P2:
    - Retry tối đa MAX_RETRY lần với exponential backoff
    - Phát hiện 429 → nghỉ dài trước khi retry
    - Kiểm tra redirect login sau mỗi lần load
    """
    url = f"{HEYETSY_BASE}/{listing_id}"

    for attempt in range(1, MAX_RETRY + 1):
        try:
            driver.get(url)
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR, "a[download*='il_fullxfull']"
                ))
            )
            smart_sleep(0.5, 1.0)
        except Exception:
            # Timeout chờ DOM — trang load chậm hoặc không có ảnh
            smart_sleep(1.0, 2.0)

        # ── Kiểm tra bị redirect về login ─────────────────────────────────────
        if "/login" in driver.current_url:
            err(f"Session hết hạn — bị redirect về login tại listing {listing_id}.")
            wait_for_login(driver)
            # Sau login lại, load lại trang listing
            try:
                driver.get(url)
                WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR, "a[download*='il_fullxfull']"
                    ))
                )
            except Exception:
                smart_sleep(1.0, 2.0)

        page_src = driver.page_source

        # ── P2: Phát hiện 429 ─────────────────────────────────────────────────
        if is_rate_limited(page_src):
            backoff = BACKOFF_BASE * attempt  # 30s, 60s, 90s
            warn(f"429 / Rate limit detected tại listing {listing_id}! "
                 f"Nghỉ {backoff}s trước retry {attempt}/{MAX_RETRY}...")
            time.sleep(backoff)
            continue  # retry

        # ── Parse ảnh ─────────────────────────────────────────────────────────
        urls = parse_image_urls(page_src)

        if urls:
            return urls

        # Không có ảnh (có thể trang chưa render xong)
        if attempt < MAX_RETRY:
            backoff = BACKOFF_BASE * attempt
            retry(f"Không có ảnh tại listing {listing_id} "
                  f"(attempt {attempt}/{MAX_RETRY}) — nghỉ {backoff}s rồi thử lại...")
            time.sleep(backoff)
            driver.refresh()

    # Hết MAX_RETRY mà vẫn không có ảnh
    warn(f"Hết {MAX_RETRY} lần thử — bỏ qua listing {listing_id}.")
    return []


# ── build_record ──────────────────────────────────────────────────────────────
def build_record(meta: Dict, image_urls: List[str]) -> Dict:
    """
    Tạo 1 record theo cấu trúc:
    shop_name, listing_id, title, tags, image_1, image_2, image_3, ...
    """
    record: Dict = {
        "shop_name":  meta.get("shop_name", ""),
        "listing_id": meta.get("listing_id", ""),
        "title":      meta.get("title", ""),
        "tags":       meta.get("tags", ""),
    }
    for i, url in enumerate(image_urls, 1):
        record[f"image_{i}"] = url
    return record


# ── load_listings_meta_from_csv ───────────────────────────────────────────────
def load_listings_meta_from_csv(
    csv_path: str,
    id_col:    str = "listing_id",
    shop_col:  str = "shop_name",
    title_col: str = "title",
    tags_col:  str = "tags",
) -> List[Dict]:
    """
    Đọc metadata từ CSV output của everbee_shop_scraper / everbee_api_scraper.
    Trả về list dict: {listing_id, shop_name, title, tags}.
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    missing = [c for c in [id_col] if c not in df.columns]
    if missing:
        raise ValueError(f"Không tìm thấy cột {missing} trong {csv_path}")

    records = []
    for _, row in df.iterrows():
        records.append({
            "listing_id": row.get(id_col,    ""),
            "shop_name":  row.get(shop_col,  ""),
            "title":      row.get(title_col, ""),
            "tags":       row.get(tags_col,  ""),
        })

    records  = [r for r in records if r["listing_id"]]
    seen_ids = set()
    unique   = []
    for r in records:
        if r["listing_id"] not in seen_ids:
            seen_ids.add(r["listing_id"])
            unique.append(r)

    info(f"Loaded {len(unique)} listings từ {csv_path}")
    return unique


# ── crawl_listing_ids ─────────────────────────────────────────────────────────
def crawl_listing_ids(
    listings_meta: List[Dict],
    headless: bool = True,
) -> pd.DataFrame:
    """
    Entry point chính.
    Nhận list metadata → crawl HeyEtsy lấy image URLs →
    lưu CSV với cấu trúc: shop_name, listing_id, title, tags, image_1, image_2, ...

    Tích hợp:
    - P1: Adaptive delay (BASE_DELAY_MIN ~ BASE_DELAY_MAX) + retry + exponential backoff
    - P2: Phát hiện 429 / rate limit → nghỉ trước khi retry
    - P3: Periodic break mỗi BREAK_EVERY listings
    """
    # ── Checkpoint resume ──────────────────────────────────────────────────────
    log: List[Dict] = []
    done_ids: List[str] = []

    if os.path.exists(CHECKPOINT_CSV):
        ans = input(
            f"{ts()} {C.tag(C.CKPT, 'CKPT')} "
            f"Tìm thấy checkpoint. Resume? (y/n): "
        ).strip().lower()
        if ans == "y":
            log, done_ids = checkpoint_load()
        else:
            os.remove(CHECKPOINT_CSV)
            info("Bắt đầu mới — đã xoá checkpoint cũ.")

    checkpoint_setup_signal(log)

    remaining = [m for m in listings_meta if m["listing_id"] not in done_ids]
    info(f"Tổng {len(listings_meta)} listings — còn lại {len(remaining)} cần crawl.")
    info(f"Config: delay={BASE_DELAY_MIN}-{BASE_DELAY_MAX}s | "
         f"retry={MAX_RETRY}x | backoff={BACKOFF_BASE}s | "
         f"break mỗi {BREAK_EVERY} listings ({BREAK_DURATION}s)")

    driver = build_driver(headless=headless)
    items_since_flush = 0

    try:
        # ── Login thủ công 1 lần trước khi crawl ──────────────────────────────
        if not wait_for_login(driver):
            err("Login thất bại — thoát.")
            driver.quit()
            sys.exit(1)

        for i, meta in enumerate(remaining, 1):
            lid = meta["listing_id"]
            info(f"[{i}/{len(remaining)}] {lid}  shop={meta['shop_name']}")

            image_urls = scrape_listing_images(driver, lid)

            if not image_urls:
                warn(f"  Không có ảnh cho listing {lid} sau {MAX_RETRY} lần thử")
            else:
                done(f"  {len(image_urls)} image URLs")

            log.append(build_record(meta, image_urls))
            items_since_flush += 1

            # ── Checkpoint flush ───────────────────────────────────────────────
            if items_since_flush >= CHECKPOINT_EVERY:
                checkpoint_flush(log)
                items_since_flush = 0

            # ── P3: Periodic break ─────────────────────────────────────────────
            if i % BREAK_EVERY == 0 and i < len(remaining):
                info(f"[P3] Đã crawl {i} listings — nghỉ {BREAK_DURATION}s để tránh rate limit...")
                time.sleep(BREAK_DURATION)
                info("Tiếp tục crawl...")
            else:
                # Delay bình thường giữa các listing
                smart_sleep(BASE_DELAY_MIN, BASE_DELAY_MAX)

    finally:
        driver.quit()
        info("Browser đã đóng.")

    # ── Final flush ────────────────────────────────────────────────────────────
    checkpoint_flush(log)

    # ── Build DataFrame: tự động align các cột image_N ────────────────────────
    df = pd.DataFrame(log)

    fixed_cols = ["shop_name", "listing_id", "title", "tags"]
    image_cols = sorted(
        [c for c in df.columns if re.match(r"^image_\d+$", c)],
        key=lambda x: int(x.split("_")[1])
    )
    other_cols = [c for c in df.columns if c not in fixed_cols + image_cols]
    df = df[fixed_cols + image_cols + other_cols].fillna("")

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    done(f"CSV saved → {OUTPUT_CSV}  ({len(df)} listings, tối đa {len(image_cols)} ảnh/listing)")

    if os.path.exists(CHECKPOINT_CSV):
        os.remove(CHECKPOINT_CSV)
        ckpt("Checkpoint đã xoá.")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    CSV_INPUT = "everbee_shop_data_listings.csv"

    listings_meta = load_listings_meta_from_csv(CSV_INPUT)

    df = crawl_listing_ids(
        listings_meta=listings_meta,
        headless=False,   # Cần False để login thủ công
    )

    print(f"\n── Preview (5 rows đầu) ──")
    print(df.head().to_string(index=False))