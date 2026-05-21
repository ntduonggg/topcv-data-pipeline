import time
import re
import random
import sys
import json
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.remote.webelement import WebElement
import pandas as pd

# ── EverBee + Etsy config ──────────────────────────────────────────────────────
URL_BASE    = "https://everbee.io"
START_URL   = "https://everbee.io/product-analytics"
OUTPUT_CSV  = "everbee_data.csv"
OUTPUT_XLSX = "everbee_data.xlsx"

MAX_ROWS      = 300   # số row tối đa muốn thu thập
SCROLL_STEP   = 400   # px mỗi lần scroll virtual scroller
SCROLL_PAUSE  = 1.4   # giây chờ sau mỗi scroll để DOM render
MAX_NO_CHANGE = 5     # dừng nếu không có row mới sau N lần scroll liên tiếp

# Mapping data-field (MuiDataGrid) → tên cột output
# Chỉ gồm các cột KHÔNG bị khóa (dựa trên HTML inspect)
UNLOCKED_FIELDS: Dict[str, str] = {
    "totalReviews":   "total_reviews",
    "listingAge":     "listing_age",
    "totalFavorites": "total_favorites",
    "avgReviews":     "avg_reviews",
    "totalViews":     "total_views",
    "shopAge":        "shop_age",
    "totalShopSales": "total_shop_sales",
    "category":       "category",
    "listingType":    "listing_type",
}

# ── Niche/sub-niche keyword map (cung cấp sau, để trống trước) ────────────────
# Cấu trúc: { "niche": ["keyword1", "keyword2", ...], ... }
# Sub-niche: { "sub_niche": { "parent": "niche", "keywords": [...] } }
NICHE_KEYWORDS: Dict[str, List[str]] = {
    # Ví dụ — thay bằng keyword thật sau:
    # "resin lamp": ["resin lamp", "epoxy lamp", "resin light", "epoxy light"],
    # "scuba diver": ["scuba", "diver", "diving", "underwater"],
}
SUB_NICHE_KEYWORDS: Dict[str, Dict] = {
    # Ví dụ:
    # "anime resin": {"parent": "resin lamp", "keywords": ["anime", "dragon", "fantasy"]},
}

COLUMN_ORDER = [
    "product", "shop_name", "price",
    "total_reviews", "listing_age", "total_favorites",
    "avg_reviews", "total_views", "shop_age", "total_shop_sales",
    "category", "listing_type",
    "etsy_url", "tags",
    "niche", "sub_niche",
    "image_url",
]

# CSS selectors cho 2 vùng DOM tách biệt (ảnh 1)
# Pinned: chứa thumbnail + product title + shop name + price
SEL_PINNED = "div.MuiDataGrid-pinnedColumns.MuiDataGrid-pinnedColumns--left"
# Scrollable: chứa các cột metrics (totalViews, shopAge, ...)
SEL_RENDER_ZONE = "div.MuiDataGrid-virtualScrollerRenderZone"


# ── build_session (dùng cho Etsy requests) ────────────────────────────────────
ETSY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.etsy.com/",
    "Connection": "keep-alive",
}

def build_session() -> requests.Session:
    """Khởi tạo requests.Session để crawl Etsy listing (trang tĩnh)."""
    s = requests.Session()
    s.headers.update(ETSY_HEADERS)
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ── build_driver (Selenium Edge cho EverBee SPA) ──────────────────────────────
def build_driver(headless: bool = False) -> webdriver.Edge:
    """Khởi tạo Edge driver, ẩn webdriver flag để tránh bot-detect."""
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
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
        "Safari/537.36 Edg/124.0.0.0"
    )
    driver = webdriver.Edge(options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


# ── text (giữ nguyên signature, đổi input type) ───────────────────────────────
def text(el: Optional[WebElement]) -> Optional[str]:
    """Lấy text từ Selenium WebElement, chuẩn hoá whitespace."""
    if not el:
        return None
    try:
        t = el.text
    except Exception:
        return None
    t = t.strip()
    return re.sub(r"\s+", " ", t) if t else None


# ── smart_sleep (giữ nguyên) ───────────────────────────────────────────────────
def smart_sleep(min_s: float = 1.2, max_s: float = 2.8):
    """Nghỉ ngẫu nhiên để giống hành vi người dùng thật."""
    time.sleep(random.uniform(min_s, max_s))


# ── get_cell: lấy text cell trong MuiDataGrid (cùng pattern retry+jitter) ──────
def get_cell(row_el: WebElement, data_field: str) -> str:
    """
    Lấy text của cell MuiDataGrid theo data-field attribute.
    Tương đương get_soup() nhưng dùng cho từng cell trong DOM.
    """
    for attempt in range(1, 4):
        try:
            cell = row_el.find_element(By.CSS_SELECTOR, f"[data-field='{data_field}']")
            val = text(cell) or ""
            return val
        except Exception:
            wait = attempt * 0.5 + random.uniform(0.1, 0.3)
            if attempt < 3:
                time.sleep(wait)
    return ""


# ── get_soup: lấy soup từ Etsy URL (cùng pattern retry+jitter) ────────────────
def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    """
    Fetch Etsy listing page và trả về BeautifulSoup.
    Cùng pattern retry/jitter như get_cell().
    """
    for attempt in range(1, 5):
        r = session.get(url, timeout=30)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = int(retry_after) if retry_after else 6 * attempt
            except ValueError:
                wait = 6 * attempt
            wait += random.uniform(0.5, 2.0)
            print(f"  [WARN] 429 tại {url} → ngủ {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    r.raise_for_status()
    return BeautifulSoup("", "lxml")


# ── parse_grid_rows: extract rows từ 2 vùng DOM tách biệt ────────────────────
def parse_grid_rows(driver: webdriver.Edge) -> List[Dict]:
    """
    Extract tất cả MuiDataGrid rows hiện đang visible trong DOM.

    EverBee tách DOM thành 2 vùng (ảnh 1):
      - pinnedColumns (--left): product title, thumbnail, shop, price
      - virtualScrollerRenderZone: các cột metrics (views, age, ...)
    Ghép 2 vùng theo data-rowindex để đảm bảo đúng row.
    """
    # ── Bước 1: thu thập pinned rows (product + shop + price) ─────────────────
    pinned_map: Dict[str, Dict] = {}   # rowindex → partial record
    try:
        pinned_zone = driver.find_element(By.CSS_SELECTOR, SEL_PINNED)
        pinned_rows = pinned_zone.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    except Exception:
        pinned_rows = []

    for row in pinned_rows:
        rowindex = row.get_attribute("data-rowindex") or ""
        record: Dict = {"_rowindex": rowindex}

        # product title: span[style*='overflow: hidden'] trong [data-field='product']
        try:
            span = row.find_element(
                By.CSS_SELECTOR,
                "[data-field='product'] span[style*='overflow']"
            )
            record["product"] = re.sub(r"\s+", " ", span.text).strip()
        except Exception:
            record["product"] = get_cell(row, "product")

        # shop name
        record["shop_name"] = get_cell(row, "shopName")

        # price
        record["price"] = get_cell(row, "price")

        # thumbnail image
        try:
            img = row.find_element(By.CSS_SELECTOR, "[data-field='product'] img")
            record["image_url"] = img.get_attribute("src") or ""
        except Exception:
            record["image_url"] = ""

        if rowindex:
            pinned_map[rowindex] = record

    # ── Bước 2: thu thập scrollable rows (metrics) ───────────────────────────
    scrollable_map: Dict[str, Dict] = {}   # rowindex → partial record
    try:
        render_zone = driver.find_element(By.CSS_SELECTOR, SEL_RENDER_ZONE)
        scrollable_rows = render_zone.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    except Exception:
        scrollable_rows = []

    for row in scrollable_rows:
        rowindex = row.get_attribute("data-rowindex") or ""
        partial: Dict = {}
        for data_field, col_name in UNLOCKED_FIELDS.items():
            partial[col_name] = get_cell(row, data_field)

        # Etsy URL: lấy từ nút "Etsy" hiện khi hover (data-field='actions' hoặc href)
        partial["etsy_url"] = extract_etsy_url_from_row(row)

        if rowindex:
            scrollable_map[rowindex] = partial

    # ── Bước 3: ghép theo rowindex ────────────────────────────────────────────
    results = []
    all_indexes = set(pinned_map.keys()) | set(scrollable_map.keys())

    for idx in sorted(all_indexes, key=lambda x: int(x) if x.isdigit() else 0):
        record = {}
        record.update(pinned_map.get(idx, {}))
        record.update(scrollable_map.get(idx, {}))
        record.pop("_rowindex", None)

        has_data = any(v for k, v in record.items() if k not in ("image_url", "etsy_url") and v)
        if has_data:
            results.append(record)

    return results


# ── extract_* helpers ─────────────────────────────────────────────────────────
def extract_etsy_url_from_row(row_el: WebElement) -> str:
    """
    Lấy Etsy listing URL từ row.

    Ưu tiên cách 2 (ảnh 3): nút "Etsy" hiện khi hover row —
    tìm <a href*='etsy.com'> hoặc <a href*='/listing/'> trong row.
    Fallback: tìm bất kỳ href chứa 'etsy.com/listing'.
    """
    for attempt in range(1, 3):
        try:
            # Cách 2: nút Etsy button (anchor với href Etsy)
            a = row_el.find_element(
                By.CSS_SELECTOR,
                "a[href*='etsy.com/listing'], a[href*='/listing/']"
            )
            href = a.get_attribute("href") or ""
            if "etsy.com" in href or "/listing/" in href:
                return href
        except Exception:
            pass
        time.sleep(0.2 * attempt)
    return ""

def extract_etsy_url_from_panel(driver: webdriver.Edge, row_el: WebElement) -> str:
    """
    Cách 1 (ảnh 2): click row → panel Listing Details hiện ra →
    lấy href từ nút 'View on Etsy'.
    Dùng làm fallback khi extract_etsy_url_from_row() không lấy được.
    """
    try:
        row_el.click()
        # Đợi panel hiện ra
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[class*='ListingDetails'], div[class*='listing-details']"))
        )
        smart_sleep(0.4, 0.8)

        # Tìm nút "View on Etsy"
        btn = driver.find_element(
            By.XPATH,
            "//a[contains(., 'View on Etsy') or contains(@href, 'etsy.com/listing')]"
        )
        url = btn.get_attribute("href") or ""

        # Đóng panel: nhấn Escape
        driver.find_element(By.TAG_NAME, "body").send_keys("\ue00c")
        smart_sleep(0.3, 0.6)
        return url
    except Exception:
        # Đóng panel nếu đang mở
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys("\ue00c")
        except Exception:
            pass
        return ""

def extract_tags_from_etsy(session: requests.Session, etsy_url: str) -> str:
    """
    Crawl trang Etsy listing, lấy keywords/tags từ
    <script type='application/ld+json'> đầu tiên (ảnh 4).

    JSON-LD Schema.org Product chứa field 'keywords' là chuỗi các tag.
    """
    if not etsy_url:
        return ""
    for attempt in range(1, 4):
        try:
            soup = get_soup(session, etsy_url)
            # Lấy script ld+json ĐẦU TIÊN (ảnh 4)
            script_tag = soup.find("script", {"type": "application/ld+json"})
            if not script_tag or not script_tag.string:
                return ""
            data = json.loads(script_tag.string)
            # keywords là chuỗi "tag1,tag2,..." hoặc list
            keywords = data.get("keywords", "")
            if isinstance(keywords, list):
                return "; ".join(k.strip() for k in keywords if k.strip())
            return re.sub(r",\s*", "; ", keywords.strip())
        except Exception as e:
            wait = 2 * attempt + random.uniform(0.5, 1.5)
            print(f"  [WARN] Etsy tag error ({etsy_url}): {e} → retry {attempt} sau {wait:.1f}s")
            time.sleep(wait)
    return ""

def extract_niche(tags: str) -> str:
    """
    Phân loại niche dựa trên từ khóa trong cột tags.
    Keyword map được cung cấp qua NICHE_KEYWORDS.
    """
    if not tags or not NICHE_KEYWORDS:
        return ""
    tags_lower = tags.lower()
    for niche, keywords in NICHE_KEYWORDS.items():
        if any(kw.lower() in tags_lower for kw in keywords):
            return niche
    return ""

def extract_sub_niche(tags: str, niche: str) -> str:
    """
    Phân loại sub-niche dựa trên tags + niche đã xác định.
    Keyword map được cung cấp qua SUB_NICHE_KEYWORDS.
    """
    if not tags or not SUB_NICHE_KEYWORDS:
        return ""
    tags_lower = tags.lower()
    for sub_niche, cfg in SUB_NICHE_KEYWORDS.items():
        if cfg.get("parent") and cfg["parent"] != niche:
            continue
        if any(kw.lower() in tags_lower for kw in cfg.get("keywords", [])):
            return sub_niche
    return ""

def extract_virtual_scroller(driver: webdriver.Edge) -> WebElement:
    """Tìm và trả về scroll container của MuiDataGrid."""
    return WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((
            By.CSS_SELECTOR, "div.MuiDataGrid-virtualScroller"
        ))
    )

def extract_dedup_key(record: Dict) -> str:
    """Tạo key dedup từ product title + shop name."""
    return f"{record.get('product', '')}|||{record.get('shop_name', '')}"


# ── scrape_* ──────────────────────────────────────────────────────────────────
def scrape_grid_page(driver: webdriver.Edge) -> List[Dict]:
    """
    Đợi grid render xong rồi extract tất cả rows visible.
    Tương đương scrape_job_detail().
    """
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.MuiDataGrid-row"))
        )
    except Exception:
        print("[ERROR] Không tìm thấy MuiDataGrid rows — kiểm tra lại trang.")
        return []
    smart_sleep(0.5, 1.0)
    return parse_grid_rows(driver)

def scrape_etsy_tags(
    session: requests.Session,
    driver: webdriver.Edge,
    rows: List[Dict],
) -> List[Dict]:
    """
    Với mỗi row đã có, lấy Etsy URL (nếu chưa có) rồi crawl tags.
    Tương đương scrape_company() trong TopCV — enrichment sau collect.

    Chiến lược lấy Etsy URL (theo thứ tự ưu tiên):
      1. Đã có từ parse_grid_rows() (nút Etsy hover - cách 2, ảnh 3)
      2. Click row → panel → nút 'View on Etsy' (cách 1, ảnh 2)
    """
    total = len(rows)
    for i, record in enumerate(rows):
        etsy_url = record.get("etsy_url", "")

        # Fallback cách 1: click row nếu chưa có URL
        if not etsy_url:
            try:
                # Tìm lại row element theo product title
                row_els = driver.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
                for row_el in row_els:
                    title_cell = get_cell(row_el, "product")
                    if title_cell and record.get("product", "") in title_cell:
                        etsy_url = extract_etsy_url_from_panel(driver, row_el)
                        break
            except Exception:
                pass
            record["etsy_url"] = etsy_url

        # Crawl tags từ Etsy
        tags = ""
        if etsy_url:
            print(f"  [{i+1}/{total}] Crawl tags: {etsy_url[:60]}...")
            tags = extract_tags_from_etsy(session, etsy_url)
            smart_sleep(1.0, 2.0)   # delay giữa các request Etsy
        else:
            print(f"  [{i+1}/{total}] Không có Etsy URL — bỏ qua tags")

        record["tags"] = tags

        # Phân loại niche / sub-niche
        record["niche"]     = extract_niche(tags)
        record["sub_niche"] = extract_sub_niche(tags, record["niche"])

    return rows

def scrape_scroll_and_collect(driver: webdriver.Edge, max_rows: int = MAX_ROWS) -> List[Dict]:
    """
    Scroll qua MuiDataGrid virtualScroller, thu thập rows không trùng lặp.
    Tương đương vòng lặp page trong crawl_to_dataframe() của TopCV.
    """
    seen: Dict[str, Dict] = {}
    no_change_count = 0

    try:
        scroller = extract_virtual_scroller(driver)
    except Exception:
        print("[ERROR] Không tìm thấy MuiDataGrid-virtualScroller.")
        return []

    print(f"[INFO] Bắt đầu scroll & collect (target: {max_rows} rows)...")

    while len(seen) < max_rows:
        batch = scrape_grid_page(driver)
        new_count = 0

        for record in batch:
            key = extract_dedup_key(record)
            if key and key not in seen:
                seen[key] = record
                new_count += 1

        print(f"  → {len(seen)} rows duy nhất  (+{new_count} mới)")

        if new_count == 0:
            no_change_count += 1
            if no_change_count >= MAX_NO_CHANGE:
                print("[INFO] Không có row mới — đã hết data hoặc bị giới hạn plan.")
                break
        else:
            no_change_count = 0

        driver.execute_script(
            "arguments[0].scrollTop += arguments[1]",
            scroller,
            SCROLL_STEP,
        )
        time.sleep(SCROLL_PAUSE + random.uniform(0, 0.4))

    return list(seen.values())


# ── crawl_to_dataframe (giữ nguyên tên + signature) ───────────────────────────
def crawl_to_dataframe(
    keyword: str = "",
    max_rows: int = MAX_ROWS,
    delay_between_pages: tuple = (0.5, 1),
) -> pd.DataFrame:
    """
    Entry point chính:
      1. Mở EverBee → chờ user login + search
      2. Scroll & collect tất cả rows (EverBee)
      3. Với mỗi row: lấy Etsy URL → crawl tags → phân loại niche
      4. Trả về DataFrame đầy đủ
    """
    rows: List[Dict] = []
    seen_keys: set = set()

    driver = build_driver(headless=False)
    session = build_session()

    try:
        # ── Bước 1: mở EverBee ────────────────────────────────────
        driver.get(START_URL)
        print(f"\n[ACTION] Browser đã mở: {START_URL}")
        print("[ACTION] Vui lòng:")
        print("  1. Đăng nhập vào tài khoản EverBee")
        if keyword:
            print(f"  2. Search keyword: '{keyword}'")
        else:
            print("  2. Search keyword bất kỳ bạn muốn research")
        print("  3. Đợi bảng Product Analytics hiện đầy đủ")
        print("\nSau đó quay lại terminal và nhấn ENTER...")
        input()

        # ── Bước 2: scroll & collect EverBee grid ─────────────────
        raw_rows = scrape_scroll_and_collect(driver, max_rows=max_rows)

        if not raw_rows:
            print("[WARN] Không thu thập được row nào.")
            return pd.DataFrame()

        # Dedup lần cuối
        for record in raw_rows:
            key = extract_dedup_key(record)
            if key and key not in seen_keys:
                seen_keys.add(key)
                rows.append(record)

        print(f"\n[INFO] {len(rows)} rows sau dedup — bắt đầu crawl Etsy tags...")

        # ── Bước 3: enrich tags + niche (dùng cả driver + session) ─
        rows = scrape_etsy_tags(session, driver, rows)

    finally:
        driver.quit()
        print("[INFO] Browser đã đóng.")

    # ── Build DataFrame + sắp xếp cột ─────────────────────────────
    df = pd.DataFrame(rows)
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    return df[cols + extra]


# ── __main__ (giữ nguyên pattern) ─────────────────────────────────────────────
if __name__ == "__main__":
    keyword = "epoxy resin night light"   # đổi keyword tuỳ ý

    df = crawl_to_dataframe(
        keyword=keyword,
        max_rows=MAX_ROWS,
        delay_between_pages=(0.5, 1),
    )

    if df.empty:
        print("[ERROR] Không có dữ liệu để lưu.")
        sys.exit(1)

    print(df.head())

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Saved CSV: {OUTPUT_CSV}")

    df.to_excel(OUTPUT_XLSX, index=False)
    print(f"Saved Excel: {OUTPUT_XLSX}")