import time
import re
import random
import sys
import json
import os
import signal
from datetime import datetime
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
CHECKPOINT_CSV      = "everbee_checkpoint.csv"   # file lưu tạm, tự động flush
CHECKPOINT_EVERY    = 500                         # flush xuống disk sau mỗi N rows mới

MAX_ROWS      = 5000   # số row tối đa muốn thu thập
SCROLL_STEP   = 500   # px mỗi lần scroll virtual scroller (tăng từ 400 → 800 để ít scroll hơn)
SCROLL_PAUSE  = 0.5   # giây chờ sau mỗi scroll để DOM render (giảm từ 1.4 → 0.5)
MAX_NO_CHANGE = 5     # dừng nếu không có row mới sau N lần scroll liên tiếp

# Mapping data-field (MuiDataGrid) → tên cột output
# Chỉ gồm các cột KHÔNG bị khóa (dựa trên HTML inspect)
UNLOCKED_FIELDS: Dict[str, str] = {
    "shopName":   "shop_name",
    "price":     "price",
    "reviews":   "total_reviews",
    "listingAge":     "listing_age",
    "totalFavourites": "total_favorites",
    "averageReviews":     "avg_reviews",
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
    "listing_age", "total_views", "views_per_month" , "total_reviews",
    "total_favorites", "avg_reviews", "shop_age", "total_shop_sales",
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

# ── ANSI color tags (dùng cho print, không cần thư viện ngoài) ────────────────
class C:
    INFO      = "\033[94m"   # xanh dương  — [INFO]
    ACTION    = "\033[94m"   # xanh dương  — [ACTION]
    WARN      = "\033[93m"   # vàng        — [WARN]
    CKPT      = "\033[92m"   # xanh lá     — [CKPT]
    ERROR     = "\033[91m"   # đỏ          — [ERROR]
    INTERRUPT = "\033[95m"   # tím         — [INTERRUPT]
    TIME      = "\033[96m"   # cyan        — [HH:MM:SS]
    DONE      = "\033[92m"   # xanh lá     — [DONE]
    RESET     = "\033[0m"
 
    @staticmethod
    def tag(color: str, label: str) -> str:
        return f"{color}[{label}]{C.RESET}"

# # ── Checkpoint: lưu/load/signal ───────────────────────────────────────────────
# def checkpoint_flush(seen: Dict[str, Dict]) -> None:
#     """
#     Ghi toàn bộ seen dict xuống CHECKPOINT_CSV (atomic write: ghi temp → rename).
#     CSV write ~5ms cho 1000 rows → không ảnh hưởng tốc độ crawl.
#     """
#     if not seen:
#         return
#     tmp = CHECKPOINT_CSV + ".tmp"
#     df = pd.DataFrame(list(seen.values()))
#     df.to_csv(tmp, index=False, encoding="utf-8-sig")
#     os.replace(tmp, CHECKPOINT_CSV)   # atomic trên cùng filesystem


# def checkpoint_load() -> Dict[str, Dict]:
#     """
#     Load checkpoint từ CHECKPOINT_CSV nếu tồn tại.
#     Trả về dict {dedup_key: record} để tiếp tục crawl từ điểm dừng.
#     """
#     if not os.path.exists(CHECKPOINT_CSV):
#         return {}
#     try:
#         df = pd.read_csv(CHECKPOINT_CSV, encoding="utf-8-sig", dtype=str).fillna("")
#         seen: Dict[str, Dict] = {}
#         for record in df.to_dict("records"):
#             key = extract_dedup_key(record)
#             if key:
#                 seen[key] = record
#         print(f"[CKPT] Resume: load {len(seen)} rows từ '{CHECKPOINT_CSV}'")
#         return seen
#     except Exception as e:
#         print(f"[WARN] Không đọc được checkpoint ({e}) — bắt đầu mới.")
#         return {}


# def checkpoint_setup_signal(seen: Dict[str, Dict]) -> None:
#     """
#     Đăng ký Ctrl+C handler: flush checkpoint rồi exit sạch.
#     Không làm mất data khi người dùng bấm Ctrl+C giữa chừng.
#     """
#     def _handler(sig, frame):
#         print("\n[INTERRUPT] Ctrl+C — đang lưu checkpoint trước khi thoát...")
#         checkpoint_flush(seen)
#         print(f"[CKPT] Đã lưu {len(seen)} rows → '{CHECKPOINT_CSV}'. Thoát.")
#         sys.exit(0)
#     signal.signal(signal.SIGINT, _handler)


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
def smart_sleep(min_s: float = 0.3, max_s: float = 0.7):
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
            wait = attempt * 0.2 + random.uniform(0.05, 0.15)
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
            print(f"  {C.tag(C.WARN, 'WARN')} 429 tại {url} → ngủ {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    r.raise_for_status()
    return BeautifulSoup("", "lxml")


# ── parse_listing_age: "17 Mo." → 17, "147 Mo." → 147 ───────────────────────
def parse_age(raw: str) -> str:
    """Trích số từ chuỗi listing age, ví dụ '17 Mo.' → '17', '3 Mo.' → '3'."""
    m = re.search(r"(\d+)", raw)
    return m.group(1) if m else raw.strip()


# ── parse_grid_rows: extract rows từ 2 vùng DOM tách biệt ────────────────────
def parse_grid_rows(driver: webdriver.Edge) -> List[Dict]:
    """
    Extract tất cả MuiDataGrid rows hiện đang visible trong DOM.

    Theo DOM thực tế của EverBee (ảnh 2):
      - pinnedColumns--left : product title + thumbnail (image_url)
      - virtualScrollerRenderZone: shop_name, price + tất cả cột metrics

    Ghép 2 vùng theo data-rowindex để đảm bảo đúng row.
    """
    # ── Bước 1: pinned rows → chỉ lấy product title + thumbnail ─────────────
    pinned_map: Dict[str, Dict] = {}   # rowindex → partial record
    try:
        pinned_zone = driver.find_element(By.CSS_SELECTOR, SEL_PINNED)
        pinned_rows = pinned_zone.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    except Exception:
        pinned_rows = []

    for row in pinned_rows:
        rowindex = row.get_attribute("data-rowindex") or ""
        record: Dict = {"_rowindex": rowindex}

        # product title: span overflow-hidden bên trong [data-field='product']
        try:
            span = row.find_element(
                By.CSS_SELECTOR,
                "[data-field='product'] span[style*='overflow']"
            )
            record["product"] = re.sub(r"\s+", " ", span.text).strip()
        except Exception:
            record["product"] = get_cell(row, "product")

        # thumbnail image
        # try:
        #     img = row.find_element(By.CSS_SELECTOR, "[data-field='product'] img")
        #     record["image_url"] = img.get_attribute("src") or ""
        # except Exception:
        #     record["image_url"] = ""

        if rowindex:
            pinned_map[rowindex] = record

    # ── Bước 2: scrollable rows → shop_name, price + metrics ────────────────
    scrollable_map: Dict[str, Dict] = {}   # rowindex → partial record
    try:
        render_zone = driver.find_element(By.CSS_SELECTOR, SEL_RENDER_ZONE)
        scrollable_rows = render_zone.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    except Exception:
        scrollable_rows = []

    for row in scrollable_rows:
        rowindex = row.get_attribute("data-rowindex") or ""
        partial: Dict = {}

        # shop_name và price nằm trong scrollable zone (xác nhận từ DOM ảnh 2)
        # partial["shop_name"] = get_cell(row, "shopName")
        # partial["price"]     = get_cell(row, "price")

        # metrics từ UNLOCKED_FIELDS
        for data_field, col_name in UNLOCKED_FIELDS.items():
            raw = get_cell(row, data_field)
            # Chuẩn hóa listing_age: "17 Mo." → 17
            if col_name == "listing_age" or col_name == "shop_age":
                raw = parse_age(raw)
            partial[col_name] = raw

        # tính views_per_month nếu có total_views + listing_age
        # total_views có thể dạng "25,170" → cần strip dấu phẩy trước khi tính
        try:
            _views = float(partial.get("total_views", "").replace(",", ""))
            _age   = float(partial.get("listing_age", "0") or "0")
            partial["views_per_month"] = round(_views / _age, 1) if _age > 0 else ""
        except (ValueError, ZeroDivisionError):
            partial["views_per_month"] = ""

        # Etsy URL từ nút hover
        # partial["etsy_url"] = extract_etsy_url_from_row(row)

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
    Lấy Etsy listing URL từ row element đang visible.

    Thử theo thứ tự:
      1. Tìm <a href*='etsy.com/listing'> trong DOM hiện tại (nhanh, ~0ms)
      2. Tìm bất kỳ href chứa '/listing/' (fallback href tương đối)
    Trả về "" nếu không tìm thấy — enrich_etsy_urls() sẽ fallback sang panel.
    """
    try:
        a = row_el.find_element(
            By.CSS_SELECTOR,
            "a[href*='etsy.com/listing'], a[href*='/listing/']"
        )
        href = a.get_attribute("href") or ""
        if href:
            return href
    except Exception:
        pass
    return ""


def enrich_etsy_urls(driver: webdriver.Edge, seen: Dict[str, Dict]) -> None:
    """
    Sau khi scroll xong, chạy qua các row đang visible và bổ sung etsy_url
    cho những record nào chưa có bằng cách:
      1. Hover vào row → nút Etsy hiện ra → lấy href (ActionChains)
      2. Nếu vẫn rỗng → click row → panel → lấy href 'View on Etsy'

    Chỉ xử lý row nào visible trên màn hình tại thời điểm gọi.
    Được gọi sau mỗi CHECKPOINT_EVERY rows hoặc khi crawl xong,
    KHÔNG chạy trong vòng scroll chính → không làm chậm scroll.
    """
    from selenium.webdriver.common.action_chains import ActionChains

    # Build lookup: dedup_key → record (chỉ những record chưa có etsy_url)
    missing = {k: v for k, v in seen.items() if not v.get("etsy_url", "").strip()}
    if not missing:
        return

    try:
        row_els = driver.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    except Exception:
        return

    enriched = 0
    for row_el in row_els:
        # Tìm product title của row này để match với seen dict
        try:
            product = row_el.find_element(
                By.CSS_SELECTOR, "[data-field='product']"
            ).text.strip()
        except Exception:
            product = ""

        # Tìm record tương ứng trong missing
        matched_key = None
        for k, v in missing.items():
            if v.get("product", "") == product and product:
                matched_key = k
                break
        if not matched_key:
            continue

        # ── Cách 1: hover để force-render nút Etsy ───────────────
        url = ""
        try:
            ActionChains(driver).move_to_element(row_el).perform()
            time.sleep(0.15)   # đợi CSS hover render nút
            url = extract_etsy_url_from_row(row_el)
        except Exception:
            pass

        # ── Cách 2: click row → panel 'View on Etsy' ─────────────
        if not url:
            url = extract_etsy_url_from_panel(driver, row_el)

        if url:
            seen[matched_key]["etsy_url"] = url
            del missing[matched_key]
            enriched += 1

        if not missing:
            break   # tất cả đã có URL, dừng sớm

    if enriched:
        print(f"  {C.tag(C.URL, 'URL')} Bổ sung etsy_url cho {enriched} rows")

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
    [DISABLED] Crawl trang Etsy listing, lấy keywords/tags từ
    <script type='application/ld+json'> đầu tiên (ảnh 4).

    JSON-LD Schema.org Product chứa field 'keywords' là chuỗi các tag.
    """
    # ── TAG CRAWLING TẠM THỜI TẮT ────────────────────────────────────────────
    # Bỏ comment phần dưới khi cần crawl tags trở lại
    #
    # if not etsy_url:
    #     return ""
    # for attempt in range(1, 4):
    #     try:
    #         soup = get_soup(session, etsy_url)
    #         # Lấy script ld+json ĐẦU TIÊN (ảnh 4)
    #         script_tag = soup.find("script", {"type": "application/ld+json"})
    #         if not script_tag or not script_tag.string:
    #             return ""
    #         data = json.loads(script_tag.string)
    #         # keywords là chuỗi "tag1,tag2,..." hoặc list
    #         keywords = data.get("keywords", "")
    #         if isinstance(keywords, list):
    #             return "; ".join(k.strip() for k in keywords if k.strip())
    #         return re.sub(r",\s*", "; ", keywords.strip())
    #     except Exception as e:
    #         wait = 2 * attempt + random.uniform(0.5, 1.5)
    #         print(f"  [WARN] Etsy tag error ({etsy_url}): {e} → retry {attempt} sau {wait:.1f}s")
    #         time.sleep(wait)
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

# ── Checkpoint helpers ────────────────────────────────────────────────────────
def checkpoint_load(path: str = CHECKPOINT_CSV) -> Dict[str, Dict]:
    """
    Load checkpoint từ CSV (nếu có) → trả về dict {dedup_key: record}.
    Gọi khi khởi động để tiếp tục session bị ngắt giữa chừng.
    """
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        seen: Dict[str, Dict] = {}
        for record in df.to_dict("records"):
            key = extract_dedup_key(record)
            if key:
                seen[key] = record
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.RESUME, 'RESUME')} Đã load {len(seen)} rows từ checkpoint: {path}")
        return seen
    except Exception as e:
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.WARN, 'WARN')} Không đọc được checkpoint ({path}): {e} — bắt đầu mới.")
        return {}


def checkpoint_flush(seen: Dict[str, Dict], path: str = CHECKPOINT_CSV) -> None:
    """
    Ghi toàn bộ seen dict xuống CSV checkpoint (overwrite).
    Được gọi định kỳ sau mỗi CHECKPOINT_EVERY rows mới — không block crawl.
    """
    if not seen:
        return
    rows = list(seen.values())
    df = pd.DataFrame(rows)
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    df[cols + extra].to_csv(path, index=False, encoding="utf-8-sig")


def checkpoint_setup_signal(seen: Dict[str, Dict], driver_ref: list = None) -> None:
    """
    Đăng ký handler cho Ctrl+C (SIGINT): flush checkpoint → exit sạch.
    driver_ref: list chứa [driver] — dùng khi bật lại enrich_etsy_urls.
    """
    def _handler(sig, frame):
        print(f"\n{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INTERRUPT, 'INTERRUPT')} Nhận Ctrl+C — đang lưu {len(seen)} rows vào checkpoint...")
        # if driver_ref and driver_ref[0]:   # [DISABLED] bật lại khi cần etsy_url
        #     try:
        #         enrich_etsy_urls(driver_ref[0], seen)
        #     except Exception:
        #         pass
        checkpoint_flush(seen)
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INTERRUPT, 'INTERRUPT')} Đã lưu → {CHECKPOINT_CSV}  |  Thoát.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


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
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.ERROR, 'ERROR')} Không tìm thấy MuiDataGrid rows — kiểm tra lại trang.")
        return []
    #smart_sleep(0.2, 0.4)
    return parse_grid_rows(driver)

def scrape_etsy_tags(
    session: requests.Session,
    driver: webdriver.Edge,
    rows: List[Dict],
) -> List[Dict]:
    """
    [DISABLED] Với mỗi row đã có, lấy Etsy URL (nếu chưa có) rồi crawl tags.

    Chiến lược lấy Etsy URL (theo thứ tự ưu tiên):
      1. Đã có từ parse_grid_rows() (nút Etsy hover - cách 2, ảnh 3)
      2. Click row → panel → nút 'View on Etsy' (cách 1, ảnh 2)
    """
    # ── TAG ENRICHMENT TẠM THỜI TẮT ──────────────────────────────────────────
    # Toàn bộ bước crawl Etsy tags + phân loại niche bị tắt để tăng tốc độ.
    # Bỏ comment phần dưới khi cần dùng lại:
    #
    # total = len(rows)
    # for i, record in enumerate(rows):
    #     etsy_url = record.get("etsy_url", "")
    #
    #     # Fallback cách 1: click row nếu chưa có URL
    #     if not etsy_url:
    #         try:
    #             row_els = driver.find_elements(By.CSS_SELECTOR, "div.MuiDataGrid-row")
    #             for row_el in row_els:
    #                 title_cell = get_cell(row_el, "product")
    #                 if title_cell and record.get("product", "") in title_cell:
    #                     etsy_url = extract_etsy_url_from_panel(driver, row_el)
    #                     break
    #         except Exception:
    #             pass
    #         record["etsy_url"] = etsy_url
    #
    #     # Crawl tags từ Etsy
    #     tags = ""
    #     if etsy_url:
    #         print(f"  [{i+1}/{total}] Crawl tags: {etsy_url[:60]}...")
    #         tags = extract_tags_from_etsy(session, etsy_url)
    #         smart_sleep(1.0, 2.0)   # delay giữa các request Etsy
    #     else:
    #         print(f"  [{i+1}/{total}] Không có Etsy URL — bỏ qua tags")
    #
    #     record["tags"] = tags
    #
    #     # Phân loại niche / sub-niche
    #     record["niche"]     = extract_niche(tags)
    #     record["sub_niche"] = extract_sub_niche(tags, record["niche"])

    # Gán tags/niche rỗng để giữ cấu trúc DataFrame
    for record in rows:
        record.setdefault("tags", "")
        record.setdefault("niche", "")
        record.setdefault("sub_niche", "")
    return rows

def scrape_scroll_and_collect(
    driver: webdriver.Edge,
    max_rows: int = MAX_ROWS,
    seen: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    """
    Scroll qua MuiDataGrid virtualScroller, thu thập rows không trùng lặp.

    Hỗ trợ checkpoint/resume:
      - seen: dict {dedup_key: record} được load từ checkpoint trước đó.
              Nếu None -> bắt đầu mới hoàn toàn.
      - Cứ mỗi CHECKPOINT_EVERY rows mới sẽ flush xuống CHECKPOINT_CSV.
      - Ctrl+C được bắt bởi signal handler -> flush rồi exit sạch.
    """
    if seen is None:
        seen = {}

    no_change_count = 0
    rows_since_flush = 0
    start_count = len(seen)

    # Đăng ký Ctrl+C handler để flush khi ngắt thủ công
    driver_ref = [driver]
    checkpoint_setup_signal(seen, driver_ref)

    try:
        scroller = extract_virtual_scroller(driver)
    except Exception:
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.ERROR, 'ERROR')} Không tìm thấy MuiDataGrid-virtualScroller.")
        return list(seen.values())

    remaining = max_rows - start_count
    print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INFO, 'INFO')} Bắt đầu scroll & collect (target: {max_rows} rows, đã có: {start_count}, cần thêm: {remaining})...")

    while len(seen) < max_rows:
        batch = scrape_grid_page(driver)
        new_count = 0

        for record in batch:
            key = extract_dedup_key(record)
            if key and key not in seen:
                seen[key] = record
                new_count += 1
                rows_since_flush += 1

        print(f" {C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))} -> {len(seen)} rows duy nhất  (+{new_count} mới)")

        # ── Flush checkpoint định kỳ ──────────────────────────────
        if rows_since_flush >= CHECKPOINT_EVERY:
            # enrich_etsy_urls(driver, seen)   # [DISABLED] bật lại khi cần lấy etsy_url
            checkpoint_flush(seen)
            print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.CKPT, 'CKPT')} Đã lưu checkpoint: {len(seen)} rows -> {CHECKPOINT_CSV}")
            rows_since_flush = 0

        if new_count == 0:
            no_change_count += 1
            if no_change_count >= MAX_NO_CHANGE:
                print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INFO, 'INFO')} Không có row mới — đã hết data hoặc bị giới hạn plan.")
                break
        else:
            no_change_count = 0

        driver.execute_script(
            "arguments[0].scrollTop += arguments[1]",
            scroller,
            SCROLL_STEP,
        )
        time.sleep(SCROLL_PAUSE + random.uniform(0, 0.2))

    # flush lần cuối khi kết thúc bình thường
    # enrich_etsy_urls(driver, seen)   # [DISABLED] bật lại khi cần lấy etsy_url
    checkpoint_flush(seen)
    print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.CKPT, 'CKPT')} Flush cuối: {len(seen)} rows -> {CHECKPOINT_CSV}")

    return list(seen.values())


# ── crawl_to_dataframe (giữ nguyên tên + signature) ───────────────────────────
def crawl_to_dataframe(
    keyword: str = "",
    max_rows: int = MAX_ROWS,
    delay_between_pages: tuple = (0.5, 1),
) -> pd.DataFrame:
    """
    Entry point chính:
      1. Hỏi resume từ checkpoint nếu có
      2. Mở EverBee → chờ user login + search
      3. Scroll & collect (flush checkpoint mỗi CHECKPOINT_EVERY rows)
      4. Ctrl+C bất kỳ lúc nào → flush + exit an toàn
      5. Khi xong → merge checkpoint vào output CSV/XLSX cuối
    """
    # ── Bước 0: kiểm tra checkpoint ───────────────────────────────
    seen: Dict[str, Dict] = {}
    if os.path.exists(CHECKPOINT_CSV):
        ans = input(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.CKPT, 'CKPT')} Tìm thấy checkpoint '{CHECKPOINT_CSV}'. Resume? (y/n): ").strip().lower()
        if ans == "y":
            seen = checkpoint_load()
        else:
            os.remove(CHECKPOINT_CSV)
            print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.CKPT, 'CKPT')} Đã xoá checkpoint cũ — bắt đầu mới.")

    rows: List[Dict] = []
    seen_keys: set = set()

    driver = build_driver(headless=False)
    session = build_session()

    try:
        # ── Bước 1: mở EverBee ────────────────────────────────────
        driver.get(START_URL)
        print(f"\n{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.ACTION, 'ACTION')} Browser đã mở: {START_URL}")
        print(f"{C.tag(C.ACTION, 'ACTION')} Vui lòng:")
        print("  1. Đăng nhập vào tài khoản EverBee")
        if keyword:
            print(f"  2. Search keyword: '{keyword}'")
        else:
            print("  2. Search keyword bất kỳ bạn muốn research")
        print("  3. Đợi bảng Product Analytics hiện đầy đủ")
        if seen:
            print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INFO, 'INFO')} (Resume mode: đã có {len(seen)} rows, scroll đến vị trí tương ứng)")
        print("\nSau đó quay lại terminal và nhấn ENTER...")
        input()

        # ── Bước 2: scroll & collect EverBee grid (với checkpoint) ─
        raw_rows = scrape_scroll_and_collect(driver, max_rows=max_rows, seen=seen)

        if not raw_rows:
            print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.WARN, 'WARN')} Không thu thập được row nào.")
            # Vẫn load checkpoint nếu có để không mất data cũ
            if os.path.exists(CHECKPOINT_CSV):
                seen = checkpoint_load()
                raw_rows = list(seen.values())
            if not raw_rows:
                return pd.DataFrame()

        # Dedup lần cuối
        for record in raw_rows:
            key = extract_dedup_key(record)
            if key and key not in seen_keys:
                seen_keys.add(key)
                rows.append(record)

        print(f"\n{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INFO, 'INFO')} {len(rows)} rows sau dedup — bỏ qua crawl Etsy tags (đang tắt).")

        # ── Bước 3: gán tags/niche rỗng (tag crawling đang tắt) ─────
        rows = scrape_etsy_tags(session, driver, rows)

    finally:
        driver.quit()
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.INFO, 'INFO')} Browser đã đóng.")

    # ── Build DataFrame + sắp xếp cột ─────────────────────────────
    df = pd.DataFrame(rows)
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    df = df[cols + extra]

    # ── Lưu output cuối + xoá checkpoint ──────────────────────────
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.DONE, 'DONE')} Saved CSV: {OUTPUT_CSV}")
    df.to_excel(OUTPUT_XLSX, index=False)
    print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.DONE, 'DONE')} Saved Excel: {OUTPUT_XLSX}")

    if os.path.exists(CHECKPOINT_CSV):
        os.remove(CHECKPOINT_CSV)
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.CKPT, 'CKPT')} Đã xoá checkpoint '{CHECKPOINT_CSV}' (không còn cần).")

    return df


# ── __main__ (giữ nguyên pattern) ─────────────────────────────────────────────
if __name__ == "__main__":
    keyword = "epoxy resin night light"   # đổi keyword tuỳ ý

    df = crawl_to_dataframe(
        keyword=keyword,
        max_rows=MAX_ROWS,
        delay_between_pages=(0.5, 1),
    )

    if df.empty:
        print(f"{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.ERROR, 'ERROR')} Không có dữ liệu để lưu.")
        sys.exit(1)

    print(f"\n{C.tag(C.TIME, datetime.now().strftime('%H:%M:%S'))}{C.tag(C.DONE, 'DONE')} Tổng cộng {len(df)} rows.")
    print(df.head())