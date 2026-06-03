import time
import re
import random
import sys
import os
import signal
from datetime import datetime
from typing import Dict, List, Tuple

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


# ── Config ────────────────────────────────────────────────────────────────────
HEYETSY_BASE     = "https://heyetsy.com/listing"
OUTPUT_CSV       = "heyetsy_image_urls.csv"
PASS_CSV         = "heyetsy_pass_{n}.csv"
CHECKPOINT_CSV   = "heyetsy_checkpoint.csv"
CHECKPOINT_EVERY = 20

MAX_PASSES       = 3

# Delay giữa các listing theo pass (pass 1 nhanh nhất)
DELAY_PASS = {
    1: (1.5, 2.5),
    2: (2.0, 3.0),
    3: (2.5, 3.5),
}
DEFAULT_DELAY = (2.5, 3.5)

PAGE_LOAD_WAIT    = 10   # giây chờ JS render
BETWEEN_PASS_WAIT = 60   # giây nghỉ giữa các pass

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
    elapsed = 0
    while elapsed < timeout:
        time.sleep(1)
        elapsed += 1
        if "/login" not in driver.current_url and "heyetsy.com" in driver.current_url:
            done("Login thành công!")
            return True
    err(f"Timeout {timeout}s — không phát hiện login.")
    return False


# ── smart_sleep ───────────────────────────────────────────────────────────────
def smart_sleep(min_s: float, max_s: float):
    time.sleep(random.uniform(min_s, max_s))


# ── is_rate_limited ───────────────────────────────────────────────────────────
def is_rate_limited(page_source: str) -> bool:
    return any(s in page_source for s in RATE_LIMIT_SIGNALS)


# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_flush(log: List[Dict], path: str = CHECKPOINT_CSV) -> None:
    if not log:
        return
    tmp = path + ".tmp"
    pd.DataFrame(log).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Flush {len(log)} records → {path}")

def checkpoint_load(path: str = CHECKPOINT_CSV) -> Tuple[List[Dict], List[str]]:
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

_log_ref: List[Dict] = []

def setup_signal(log: List[Dict]) -> None:
    global _log_ref
    _log_ref = log
    def _handler(sig, frame):
        stop("Ctrl+C — flush checkpoint rồi thoát...")
        checkpoint_flush(_log_ref)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── parse_image_urls ──────────────────────────────────────────────────────────
def parse_image_urls(page_source: str) -> List[str]:
    soup = BeautifulSoup(page_source, "lxml")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "i.etsystatic.com" in href and "il_fullxfull" in href and href not in seen:
            urls.append(href)
            seen.add(href)
    return urls


# ── scrape_one ────────────────────────────────────────────────────────────────
def scrape_one(driver: webdriver.Edge, listing_id: str) -> List[str]:
    """
    Crawl 1 listing — không retry inline.
    429 → trả về [] để pass sau xử lý.
    Redirect login → re-login rồi crawl lại 1 lần.
    """
    url = f"{HEYETSY_BASE}/{listing_id}"

    def _load():
        try:
            driver.get(url)
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[download*='il_fullxfull']"))
            )
            smart_sleep(0.5, 1.0)
        except Exception:
            smart_sleep(1.0, 1.5)

    _load()

    if "/login" in driver.current_url:
        err(f"Session hết hạn tại listing {listing_id} — re-login...")
        wait_for_login(driver)
        _load()

    page_src = driver.page_source

    if is_rate_limited(page_src):
        warn(f"429 detected tại listing {listing_id} — sẽ retry ở pass sau.")
        return []

    return parse_image_urls(page_src)


# ── build_record ──────────────────────────────────────────────────────────────
def build_record(meta: Dict, image_urls: List[str]) -> Dict:
    record = {
        "shop_name":  meta.get("shop_name", ""),
        "listing_id": meta.get("listing_id", ""),
        "title":      meta.get("title", ""),
        "tags":       meta.get("tags", ""),
    }
    for i, url in enumerate(image_urls, 1):
        record[f"image_{i}"] = url
    return record


# ── align_df ──────────────────────────────────────────────────────────────────
def align_df(records: List[Dict]) -> pd.DataFrame:
    df     = pd.DataFrame(records)
    fixed  = ["shop_name", "listing_id", "title", "tags"]
    images = sorted(
        [c for c in df.columns if re.match(r"^image_\d+$", c)],
        key=lambda x: int(x.split("_")[1])
    )
    other  = [c for c in df.columns if c not in fixed + images]
    return df[fixed + images + other].fillna("")


# ── run_pass ──────────────────────────────────────────────────────────────────
def run_pass(
    driver:        webdriver.Edge,
    listings_meta: List[Dict],
    pass_num:      int,
) -> List[Dict]:
    """Chạy 1 pass — trả về list records (kể cả row không có ảnh)."""
    delay_min, delay_max = DELAY_PASS.get(pass_num, DEFAULT_DELAY)
    ckpt_path = CHECKPOINT_CSV

    log: List[Dict]     = []
    done_ids: List[str] = []

    if os.path.exists(ckpt_path):
        ans = input(
            f"{ts()} {C.tag(C.CKPT, 'CKPT')} "
            f"Tìm thấy checkpoint (pass {pass_num}). Resume? (y/n): "
        ).strip().lower()
        if ans == "y":
            log, done_ids = checkpoint_load(ckpt_path)
        else:
            os.remove(ckpt_path)
            info("Bắt đầu mới — đã xoá checkpoint cũ.")

    setup_signal(log)

    remaining = [m for m in listings_meta if m["listing_id"] not in done_ids]
    info(f"Pass {pass_num}: {len(listings_meta)} listings — còn lại {len(remaining)} cần crawl "
         f"| delay={delay_min}-{delay_max}s")

    items_since_flush = 0

    for i, meta in enumerate(remaining, 1):
        lid = meta["listing_id"]
        info(f"  [{i}/{len(remaining)}] {lid}  shop={meta['shop_name']}")

        image_urls = scrape_one(driver, lid)

        if image_urls:
            done(f"    {len(image_urls)} image URLs")
        else:
            warn(f"    Không có ảnh")

        log.append(build_record(meta, image_urls))
        items_since_flush += 1

        if items_since_flush >= CHECKPOINT_EVERY:
            checkpoint_flush(log, ckpt_path)
            items_since_flush = 0

        smart_sleep(delay_min, delay_max)

    checkpoint_flush(log, ckpt_path)

    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
        ckpt("Checkpoint pass đã xoá.")

    return log


# ── merge_passes ──────────────────────────────────────────────────────────────
def merge_passes(pass_results: List[List[Dict]]) -> List[Dict]:
    """
    Merge theo nguyên tắc: pass sau ghi đè pass trước
    chỉ khi pass sau tìm được ảnh.
    """
    merged: Dict[str, Dict] = {}
    for pass_log in pass_results:
        for record in pass_log:
            lid = record.get("listing_id", "")
            if not lid:
                continue
            if lid not in merged or record.get("image_1", ""):
                merged[lid] = record
    return list(merged.values())


# ── get_missing_meta ──────────────────────────────────────────────────────────
def get_missing_meta(pass_log: List[Dict], all_meta: Dict[str, Dict]) -> List[Dict]:
    """Lọc listing chưa có ảnh từ pass log."""
    return [
        all_meta[r["listing_id"]]
        for r in pass_log
        if not r.get("image_1", "") and r.get("listing_id", "") in all_meta
    ]


# ── load_listings_meta_from_csv ───────────────────────────────────────────────
def load_listings_meta_from_csv(
    csv_path:  str,
    id_col:    str = "listing_id",
    shop_col:  str = "shop_name",
    title_col: str = "title",
    tags_col:  str = "tags",
) -> List[Dict]:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    if id_col not in df.columns:
        raise ValueError(f"Không tìm thấy cột '{id_col}' trong {csv_path}")

    records, seen = [], set()
    for _, row in df.iterrows():
        lid = row.get(id_col, "")
        if lid and lid not in seen:
            seen.add(lid)
            records.append({
                "listing_id": lid,
                "shop_name":  row.get(shop_col,  ""),
                "title":      row.get(title_col, ""),
                "tags":       row.get(tags_col,  ""),
            })

    info(f"Loaded {len(records)} listings từ {csv_path}")
    return records


# ── crawl_multipass ───────────────────────────────────────────────────────────
def crawl_multipass(
    listings_meta: List[Dict],
    headless:      bool = False,
    max_passes:    int  = MAX_PASSES,
) -> pd.DataFrame:
    """
    Entry point chính.

    Pass 1 : crawl toàn bộ listings
    Pass 2 : chỉ crawl listing chưa có ảnh từ pass 1
    Pass 3 : chỉ crawl listing chưa có ảnh từ pass 2
    → Merge tất cả pass → lưu OUTPUT_CSV
    """
    all_meta_map  = {m["listing_id"]: m for m in listings_meta}
    pass_results: List[List[Dict]] = []

    driver = build_driver(headless=headless)

    try:
        if not wait_for_login(driver):
            err("Login thất bại — thoát.")
            driver.quit()
            sys.exit(1)

        current_batch = listings_meta  # pass 1: toàn bộ

        for pass_num in range(1, max_passes + 1):
            if not current_batch:
                info(f"Pass {pass_num}: không còn listing nào thiếu ảnh — dừng sớm.")
                break

            print(f"\n{'═'*60}")
            info(f"BẮT ĐẦU PASS {pass_num}/{max_passes} — {len(current_batch)} listings")
            print(f"{'═'*60}\n")

            pass_log = run_pass(driver, current_batch, pass_num)
            pass_results.append(pass_log)

            # Lưu CSV riêng mỗi pass để debug
            pass_csv = PASS_CSV.format(n=pass_num)
            align_df(pass_log).to_csv(pass_csv, index=False, encoding="utf-8-sig")
            ckpt(f"Pass {pass_num} saved → {pass_csv}")

            found   = sum(1 for r in pass_log if r.get("image_1", ""))
            missing = len(pass_log) - found
            info(f"Pass {pass_num} kết quả: {found} có ảnh | {missing} vẫn thiếu ảnh")

            if pass_num < max_passes and missing > 0:
                current_batch = get_missing_meta(pass_log, all_meta_map)
                info(f"Nghỉ {BETWEEN_PASS_WAIT}s trước pass {pass_num + 1}...")
                time.sleep(BETWEEN_PASS_WAIT)
            else:
                break

    finally:
        driver.quit()
        info("Browser đã đóng.")

    # ── Merge & save ───────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    info("MERGE kết quả tất cả pass...")
    merged = merge_passes(pass_results)
    df     = align_df(merged)

    total   = len(df)
    has_img = (df["image_1"] != "").sum()
    no_img  = total - has_img
    img_cols = [c for c in df.columns if re.match(r"^image_\d+$", c)]

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"{'═'*60}")
    done(f"OUTPUT → {OUTPUT_CSV}")
    info(f"  Tổng listings      : {total}")
    info(f"  Có ảnh             : {has_img}")
    info(f"  Vẫn thiếu ảnh      : {no_img}")
    info(f"  Số ảnh tối đa/card : {len(img_cols)}")
    print(f"{'═'*60}\n")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    CSV_INPUT = "everbee_shop_data_listings.csv"

    listings_meta = load_listings_meta_from_csv(CSV_INPUT)

    df = crawl_multipass(
        listings_meta=listings_meta,
        headless=False,
        max_passes=MAX_PASSES,
    )

    print("── Preview (5 rows đầu) ──")
    print(df.head().to_string(index=False))