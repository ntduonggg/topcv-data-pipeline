import time
import re
import random
import sys
import os
import signal
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import pandas as pd
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

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


# ── Config ────────────────────────────────────────────────────────────────────
API_BASE  = "https://api.everbee.com/product_analytics"
OUTPUT_CSV       = "everbee_data.csv"
OUTPUT_XLSX      = "everbee_data.xlsx"
CHECKPOINT_CSV   = "everbee_checkpoint.csv"
CHECKPOINT_EVERY = 1000   # flush mỗi N rows mới

PER_PAGE      = 20    # giữ 20 (giá trị server dùng), tăng nếu server cho phép
REQUEST_DELAY = 0.3   # giây giữa các request
MAX_RETRIES   = 5     # số lần retry khi lỗi
TOKEN_TIMEOUT = 120   # giây chờ browser lấy token

# ── TOKEN config ─────────────────────────────────────────────────────────────
# EverBee dùng header X-Access-Token (không phải Authorization: Bearer)
# Cách lấy thủ công:
#   DevTools → Network → request product_analytics
#   → Headers → X-Access-Token → copy toàn bộ giá trị
# Cách 2: để rỗng "" → script tự động mở browser lấy token
# Cách 3: token hết hạn giữa chừng → script tự động refresh (không cần can thiệp)
AUTH_TOKEN = ""

# ── API request params ────────────────────────────────────────────────────────
# Các params này lấy từ URL request trong DevTools
DEFAULT_PARAMS = {
    "type_of_search": "false",
    "time_range":     "last_1_month",
    "order_by":       "views",
    "order_direction":"desc",
    "per_page":       PER_PAGE,
}

# ── Order-by combos để vượt giới hạn 500 pages/query ────────────────────────
# EverBee giới hạn tối đa 500 pages (10k rows) mỗi query.
# Mỗi order_by khác nhau → ranking khác → listing mới ở các page sau.
# dedup tự động xử lý overlap giữa các combo.
QUERY_COMBOS: List[Dict] = [
    {"order_by": "views",                 "order_direction": "desc"},
    {"order_by": "est_mo_revenue",        "order_direction": "desc"},
    {"order_by": "reviews",               "order_direction": "desc"},
    {"order_by": "favorites",             "order_direction": "desc"},
    {"order_by": "listing_age",           "order_direction": "desc"},
    {"order_by": "est_reviews",           "order_direction": "desc"},
    {"order_by": "est_reviews_in_months", "order_direction": "desc"},
    {"order_by": "transaction_sold_count","order_direction": "desc"},
    {"order_by": "listing_age",           "order_direction": "asc"},
    {"order_by": "transaction_sold_count","order_direction": "asc"},
    {"order_by": "est_reviews_in_months", "order_direction": "asc"},
    {"order_by": "listing_age",           "order_direction": "asc"},
    {"order_by": "views",                 "order_direction": "asc"},
]

# ── Field mapping: API response → tên cột output ─────────────────────────────
# Dựa trên JSON response thực tế (ảnh DevTools)
FIELD_MAP: Dict[str, str] = {
    "listing_id":                    "listing_id",
    "title":                         "product",
    "price":                         "price",
    "listing_age_in_months":         "listing_age",
    "views":                         "total_views",
    "num_favorers":                  "total_favorites",
    "est_reviews":                   "total_reviews",
    "est_reviews_in_months":         "avg_reviews",
    "shop_age_month":                "shop_age",
    "transaction_sold_count":        "total_shop_sales",
    "main_category":                 "category",
    "listing_type":                  "listing_type",
    "url":                           "etsy_url",
    "shipping_from_country_iso":     "ship_from",
    "cached_visibility_score":       "visibility_score",
    #"created_at":                    "created_at",
    # Locked fields — trả về "Please upgrade", vẫn lưu để biết
    "est_mo_sales":                  "mo_sales",
    "est_mo_revenue":                "mo_revenue",
    "est_total_sales":               "total_sales_est",
    "growth_rate":                   "growth_rate",
}

# Locked fields — thay "Please upgrade" bằng rỗng
LOCKED_FIELDS = {"mo_sales", "mo_revenue", "total_sales_est", "growth_rate", "visibility_score"}

COLUMN_ORDER = [
    "listing_id", "product", "shop_name", "price",
    "listing_age", "created_age", "total_views", "views_per_month",
    "total_reviews", "avg_reviews", "total_favorites", 
    #"created_at", 
    "shop_age", "total_shop_sales",
    "category", "listing_type", "ship_from",
    "etsy_url", "tags", "image_url","engagement_rate", 
    "competition_score", "mo_sales", "mo_revenue", 
    "total_sales_est", "growth_rate", "niche", "sub_niche",
]

# Niche/sub-niche keyword map — điền sau khi có keyword list
NICHE_KEYWORDS: Dict[str, List[str]] = {}
SUB_NICHE_KEYWORDS: Dict[str, Dict] = {}


# ── get_token_via_browser ────────────────────────────────────────────────────
def get_token_via_browser() -> str:
    """
    Mở Edge browser → user login EverBee → đọc performance log để lấy token.
    Dùng khi AUTH_TOKEN chưa set hoặc khi auto-refresh sau 401.

    Fix: Selenium chuẩn không có add_cdp_listener.
    Thay bằng: bật performance logging → poll get_log("performance")
    → parse Network.requestWillBeSent events → lấy Authorization header.
    """
    options = Options()
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # Bật performance logging để capture network events
    options.set_capability("ms:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Edge(options=options)
    # Bật CDP Network tracking
    driver.execute_cdp_cmd("Network.enable", {})
    driver.get("https://app.everbee.com/product-analytics")

    print(f"\n{ts()} {C.tag(C.ACTION, 'ACTION')} Browser đã mở.")
    print(f"{ts()} {C.tag(C.ACTION, 'ACTION')} Login EverBee + search bất kỳ keyword.")
    info(f"Token sẽ tự động lấy khi request API xuất hiện (timeout {TOKEN_TIMEOUT}s)...")

    token = ""
    elapsed = 0.0

    while elapsed < TOKEN_TIMEOUT:
        time.sleep(0.5)
        elapsed += 0.5

        # Đọc performance log — mỗi lần get_log() trả về entries mới (consumed)
        try:
            logs = driver.get_log("performance")
        except Exception:
            continue

        for entry in logs:
            try:
                msg = json.loads(entry.get("message", "{}"))
                params = msg.get("message", {}).get("params", {})

                # Chỉ xử lý event requestWillBeSent
                if msg.get("message", {}).get("method") != "Network.requestWillBeSent":
                    continue

                req_url = params.get("request", {}).get("url", "")
                if "everbee." not in req_url:
                    continue

                headers = params.get("request", {}).get("headers", {})
                token_val = (
                    headers.get("X-Access-Token") or
                    headers.get("x-access-token") or ""
                )
                if token_val and "everbee." in req_url:
                    token = token_val
                    break
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        if token:
            break

    try:
        driver.quit()
    except Exception:
        pass

    if not token:
        err(f"Không lấy được token sau {TOKEN_TIMEOUT}s.")
        raise RuntimeError("Token timeout")

    done("Token lấy thành công.")
    return token


# ── build_session ─────────────────────────────────────────────────────────────
def build_session(auth_token: str) -> requests.Session:
    """Tạo requests.Session với auth header + retry logic."""
    s = requests.Session()
    s.headers.update({
        "X-Access-Token": auth_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
        ),
        "Origin":  "https://app.everbee.com",
        "Referer": "https://app.everbee.com/",
    })
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_flush(seen: Dict[str, Dict], path: str = CHECKPOINT_CSV) -> None:
    """Atomic write: ghi .tmp → rename, không corrupt khi crash."""
    if not seen:
        return
    tmp = path + ".tmp"
    df = pd.DataFrame(list(seen.values()))
    cols  = [c for c in COLUMN_ORDER if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    df[cols + extra].to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Flush {len(seen)} rows → {path}")

def checkpoint_load(path: str = CHECKPOINT_CSV) -> Dict[str, Dict]:
    """Load seen dict từ checkpoint CSV."""
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        seen: Dict[str, Dict] = {}
        for record in df.to_dict("records"):
            key = extract_dedup_key(record)
            if key:
                seen[key] = record
        ckpt(f"Loaded {len(seen)} rows từ checkpoint: {path}")
        return seen
    except Exception as e:
        warn(f"Không đọc được checkpoint: {e} — bắt đầu mới.")
        return {}

def checkpoint_setup_signal(seen: Dict[str, Dict]) -> None:
    """Ctrl+C → flush checkpoint → exit sạch."""
    def _handler(sig, frame):
        stop(f"Ctrl+C — flush {len(seen)} rows rồi thoát...")
        checkpoint_flush(seen)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── parse helpers ─────────────────────────────────────────────────────────────
def extract_dedup_key(record: Dict) -> str:
    return f"{record.get('listing_id', '')}|||{record.get('product', '')}"

def parse_record(item: Dict) -> Dict:
    """
    Map 1 item từ API response → record dict theo FIELD_MAP.
    Xử lý: tags array, shop nested object, computed views_per_month,
    locked fields, image_url.
    """
    record: Dict = {}

    # ── Flat fields theo FIELD_MAP ────────────────────────────────────────────
    for api_field, col_name in FIELD_MAP.items():
        val = item.get(api_field, "")
        # Locked field → gán rỗng
        if col_name in LOCKED_FIELDS and val == "Please upgrade":
            val = ""
        record[col_name] = str(val) if val is not None else ""

    # ── shop nested object ────────────────────────────────────────────────────
    shop = item.get("shop") or {}
    record["shop_name"] = str(shop.get("shop_name") or item.get("shop_name", ""))

    # ── tags: array → chuỗi "tag1; tag2; ..." ────────────────────────────────
    tags = item.get("tags", [])
    if isinstance(tags, list):
        record["tags"] = "; ".join(t.strip() for t in tags if t)
    else:
        record["tags"] = str(tags)

    # ── image_url: lấy từ Images array hoặc icon_url ─────────────────────────
    images = item.get("Images") or item.get("images") or []
    if isinstance(images, list) and images:
        first_img = images[0]
        record["image_url"] = (
            first_img.get("url_170x135") or
            first_img.get("url_fullxfull") or
            first_img.get("url") or ""
        ) if isinstance(first_img, dict) else str(first_img)
    else:
        record["image_url"] = str(item.get("icon_url_fullxfull") or "")

    # ── views_per_month (computed) ────────────────────────────────────────────
    try:
        _views = float(str(record.get("total_views", "0")).replace(",", ""))
        _age   = float(record.get("listing_age", "0") or "0")
        record["views_per_month"] = str(round(_views / _age, 1)) if _age > 0 else ""
    except (ValueError, ZeroDivisionError):
        record["views_per_month"] = ""

    # ── engagement_rate: favorites / views (computed) ─────────────────────────
    # Cao → listing hấp dẫn nhưng chưa đủ traffic → unmet demand
    try:
        _favs  = float(str(record.get("total_favorites", "0")).replace(",", ""))
        _views2 = float(str(record.get("total_views", "0")).replace(",", ""))
        record["engagement_rate"] = str(round(_favs / _views2, 4)) if _views2 > 0 else ""
    except (ValueError, ZeroDivisionError):
        record["engagement_rate"] = ""

    # ── competition_score: total_reviews / views_per_month (computed) ─────────
    # Thấp → ít đối thủ mạnh dù có traffic → cơ hội tốt
    try:
        _reviews = float(str(record.get("total_reviews", "0")).replace(",", ""))
        _vpm     = float(record.get("views_per_month", "0") or "0")
        record["competition_score"] = str(round(_reviews / _vpm, 2)) if _vpm > 0 else ""
    except (ValueError, ZeroDivisionError):
        record["competition_score"] = ""

    # — created_age: (now - created_at) in months (computed) ————————————
    # Listing càng mới → traffic/tháng càng đáng tin cậy hơn "Listing Age" làm tròn
    try:
        _created_at = record.get("created_at", "")
        if _created_at:
            _now = datetime.now(timezone.utc)
            _created_dt = datetime.fromisoformat(_created_at.replace("Z", "+00:00"))
            record["created_age"] = str(round((_now - _created_dt).days / 30.44, 1))
        else:
            record["created_age"] = ""
    except (ValueError, TypeError):
        record["created_age"] = ""

    # ── niche / sub-niche ─────────────────────────────────────────────────────
    record["niche"]     = extract_niche(record.get("tags", ""))
    record["sub_niche"] = extract_sub_niche(record.get("tags", ""), record["niche"])

    return record

def extract_niche(tags: str) -> str:
    if not tags or not NICHE_KEYWORDS:
        return ""
    tl = tags.lower()
    for niche, kws in NICHE_KEYWORDS.items():
        if any(k.lower() in tl for k in kws):
            return niche
    return ""

def extract_sub_niche(tags: str, niche: str) -> str:
    if not tags or not SUB_NICHE_KEYWORDS:
        return ""
    tl = tags.lower()
    for sub, cfg in SUB_NICHE_KEYWORDS.items():
        if cfg.get("parent") and cfg["parent"] != niche:
            continue
        if any(k.lower() in tl for k in cfg.get("keywords", [])):
            return sub
    return ""


# ── Custom exception ─────────────────────────────────────────────────────────
class _TokenExpired(Exception):
    """Raised khi API trả về 401 — signal để caller refresh token."""
    pass


# ── API fetch ─────────────────────────────────────────────────────────────────
def fetch_page(
    session: requests.Session,
    search_term: str,
    page: int,
    params: Optional[Dict] = None,
) -> Tuple[List[Dict], int]:
    """
    Fetch 1 trang từ API. Trả về (items, total_pages).
    Xử lý 401 (token hết hạn) và 429 (rate limit).
    """
    p = {**DEFAULT_PARAMS, **(params or {}), "search_term": search_term, "page": page}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(API_BASE, params=p, timeout=30)

            if r.status_code == 401:
                warn("Token hết hạn (401) — cần refresh.")
                raise _TokenExpired()

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 10 * attempt))
                warn(f"Rate limit (429) → ngủ {wait:.0f}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if r.status_code == 500:
                warn(f"500 Server Error tại page {page} — đã chạm giới hạn page của query này.")
                return [], -1, 0   # -1 = signal dừng combo hiện tại

            r.raise_for_status()
            data = r.json()
            items       = data.get("results", [])
            total_pages = int(data.get("total_pages", 1))
            total_count = int(data.get("total_count", 0))
            return items, total_pages, total_count

        except requests.exceptions.ConnectionError as e:
            if "NameResolutionError" in str(e) or "Failed to resolve" in str(e):
                err(f"Không resolve được host: {API_BASE}")
                err("Kiểm tra: (1) kết nối internet, (2) URL đúng chưa, (3) thử ping api.everbee.com")
                raise
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Connection error: {e} → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Request error: {e} → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)

    err(f"Không fetch được trang {page} sau {MAX_RETRIES} lần retry.")
    return [], 0, 0


# ── crawl_keyword ─────────────────────────────────────────────────────────────
def crawl_keyword(
    session: requests.Session,
    search_term: str,
    max_rows: int,
    seen: Dict[str, Dict],
    params: Optional[Dict] = None,
    combo_label: str = "",
) -> Dict[str, Dict]:
    """
    Crawl toàn bộ pages cho 1 keyword + 1 order_by combo, dedup vào seen dict.
    Dừng sớm nếu nhận signal -1 (chạm giới hạn page 500 của server).
    Flush checkpoint mỗi CHECKPOINT_EVERY rows mới.
    """
    rows_since_flush = 0
    page = 1
    label = f"[{combo_label}] " if combo_label else ""

    # Fetch trang đầu để biết total_pages
    items, total_pages, total_count = fetch_page(session, search_term, page, params)
    if not items:
        if total_pages == -1:
            warn(f"{label}Server trả về 500 ngay trang đầu — bỏ qua combo này.")
        else:
            warn(f"{label}Không có kết quả cho keyword: '{search_term}'")
        return seen

    # Cap total_pages ở 500 (giới hạn server)
    effective_pages = min(total_pages, 500)
    info(f"{label}Keyword '{search_term}': {total_count} listings, "
         f"{total_pages} trang (crawl tối đa {effective_pages})")

    while page <= effective_pages and len(seen) < max_rows:
        if page > 1:
            items, sig, _ = fetch_page(session, search_term, page, params)
            if sig == -1:
                warn(f"{label}Chạm giới hạn tại page {page} — dừng combo này.")
                break
            if not items:
                break

        new_count = 0
        for item in items:
            record = parse_record(item)
            key = extract_dedup_key(record)
            if key and key not in seen:
                seen[key] = record
                new_count += 1
                rows_since_flush += 1

        print(
            f"  {ts()} {label}page {page:>4}/{effective_pages}"
            f"  total {C.tag(C.INFO, str(len(seen)))} rows"
            f"  ({C.tag(C.CKPT, '+' + str(new_count))} mới)"
        )

        # Flush checkpoint định kỳ
        if rows_since_flush >= CHECKPOINT_EVERY:
            checkpoint_flush(seen)
            rows_since_flush = 0

        page += 1
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.1))

    return seen


# ── crawl_to_dataframe (entry point, giữ tên như scraper cũ) ─────────────────
def crawl_to_dataframe(
    keywords: List[str],
    max_rows: int = 30000,
    params: Optional[Dict] = None,
) -> pd.DataFrame:
    """
    Entry point chính.
    keywords: list keyword để crawl tuần tự, gộp chung 1 seen dict.
    max_rows: tổng số rows tối đa (cross-keyword).
    """
    # ── Lấy token ────────────────────────────────────────────────────────────
    # Cách 1: đã paste sẵn → dùng luôn
    # Cách 2: chưa set → tự động mở browser
    auth_token = AUTH_TOKEN
    if not auth_token:
        info("Chưa có token — tự động mở browser để lấy...")
        auth_token = get_token_via_browser()

    # ── Checkpoint resume ─────────────────────────────────────────────────────
    seen: Dict[str, Dict] = {}
    if os.path.exists(CHECKPOINT_CSV):
        ans = input(
            f"{ts()} {C.tag(C.CKPT, 'CKPT')} "
            f"Tìm thấy checkpoint ({CHECKPOINT_CSV}). Resume? (y/n): "
        ).strip().lower()
        if ans == "y":
            seen = checkpoint_load()
        else:
            os.remove(CHECKPOINT_CSV)
            info("Bắt đầu mới — đã xoá checkpoint cũ.")

    # ── Setup Ctrl+C ──────────────────────────────────────────────────────────
    checkpoint_setup_signal(seen)

    session = build_session(auth_token)

    # ── Crawl từng keyword × từng order_by combo ────────────────────────────────
    combos = QUERY_COMBOS
    total_combos = len(combos)

    for i, kw in enumerate(keywords, 1):
        if len(seen) >= max_rows:
            info(f"Đã đủ {max_rows} rows — dừng.")
            break
        info(f"[kw {i}/{len(keywords)}] '{kw}'  —  {total_combos} combos  (đang có {len(seen)} rows)")

        for j, combo in enumerate(combos, 1):
            if len(seen) >= max_rows:
                break
            combo_label = f"order={combo['order_by']}"
            info(f"  [{j}/{total_combos}] {combo_label}")

            # Merge combo vào params, ưu tiên params truyền vào
            merged_params = {**combo, **(params or {})}

            # Auto-refresh token khi 401
            for _token_attempt in range(1, 4):
                try:
                    seen = crawl_keyword(
                        session, kw, max_rows, seen,
                        params=merged_params,
                        combo_label=combo_label,
                    )
                    break
                except _TokenExpired:
                    if _token_attempt >= 3:
                        err("Token hết hạn 3 lần liên tiếp — dừng.")
                        checkpoint_flush(seen)
                        sys.exit(1)
                    warn(f"Token hết hạn — tự động refresh (lần {_token_attempt})...")
                    auth_token = get_token_via_browser()
                    session = build_session(auth_token)

            # Flush sau mỗi combo
            checkpoint_flush(seen)

        info(f"[kw {i}/{len(keywords)}] '{kw}' hoàn tất — tổng {len(seen)} rows unique.")

    if not seen:
        warn("Không có dữ liệu.")
        return pd.DataFrame()

    # ── Build DataFrame ───────────────────────────────────────────────────────
    df = pd.DataFrame(list(seen.values()))
    cols  = [c for c in COLUMN_ORDER if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    df = df[cols + extra]

    # ── Lưu output ───────────────────────────────────────────────────────────
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    done(f"CSV saved → {OUTPUT_CSV}  ({len(df)} rows)")

    df.to_excel(OUTPUT_XLSX, index=False)
    done(f"Excel saved → {OUTPUT_XLSX}")

    if os.path.exists(CHECKPOINT_CSV):
        os.remove(CHECKPOINT_CSV)
        ckpt("Checkpoint đã xoá.")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    KEYWORDS = [
        "resin lamp",
        # thêm keyword tại đây
    ]

    df = crawl_to_dataframe(
        keywords=KEYWORDS,
        max_rows=50000,
    )

    if not df.empty:
        done(f"Tổng cộng {len(df)} rows.")
        print(df.head().to_string(index=False))