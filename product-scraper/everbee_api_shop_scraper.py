import time
import re
import random
import sys
import os
import signal
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import pandas as pd
from selenium import webdriver
from selenium.webdriver.edge.options import Options

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
API_BASE         = "https://api.everbee.com/shops/analyze_shop"
OUTPUT_CSV       = "everbee_shop_data.csv"
OUTPUT_XLSX      = "everbee_shop_data.xlsx"
CHECKPOINT_CSV   = "everbee_shop_checkpoint.csv"
CHECKPOINT_EVERY = 5      # flush mỗi N shops (shop ít hơn listing)

PER_PAGE      = 20
REQUEST_DELAY = 0.3
MAX_RETRIES   = 5
TOKEN_TIMEOUT = 120

# Token — paste thủ công hoặc để rỗng để tự động lấy
AUTH_TOKEN = ""

DEFAULT_PARAMS = {
    "per_page": PER_PAGE,
    "order_by": "views",
    "order_direction": "desc",
}

# ── LOCKED fields — thay "Please upgrade" bằng rỗng ─────────────────────────
LOCKED_VALUES = {"Please upgrade"}

# ── Shop-level fields (từ shop_details) ──────────────────────────────────────
SHOP_FIELDS: Dict[str, str] = {
    "shop_name":                  "shop_name",
    "shop_id":                    "shop_id",
    "review_count":               "shop_review_count",
    "review_average":             "shop_review_avg",
    "transaction_sold_count":     "shop_total_sold",
    "shop_age_month":             "shop_age_month",
    "shipping_from_country_iso":  "ship_from",
    "listing_active_count":       "listing_active_count",
    "num_favorers":               "shop_favorites",
    "views":                      "shop_views",
    "revenue":                    "shop_revenue",
    "revenue_30_days":            "shop_revenue_30d",
    "sales_30_days":              "shop_sales_30d",
    "conversion_rate":            "shop_conversion_rate",
    "review_rate":                "shop_review_rate",
    "average_listing_price":      "shop_avg_listing_price",
    "sale_per_listing":           "shop_sale_per_listing",
    "shop_type":                  "shop_type",
    "category":                   "shop_category",
    "url":                        "shop_url",
    "shop_logo":                  "shop_logo_url",
    "is_deactivated":             "is_deactivated",
}

# ── Listing-level fields (từ results[]) ──────────────────────────────────────
LISTING_FIELDS: Dict[str, str] = {
    "listing_id":                    "listing_id",
    "title":                         "title",
    "url":                           "etsy_url",
    "Images":                        "image_url",
    # "price":                         "price",
    # "state":                         "state",
    # "cached_listing_age_in_months":  "listing_age",
    # "views":                         "views",
    # "num_favorers":                  "favorites",
    # "review_count":                  "reviews",
    # "cached_est_reviews_in_months":  "avg_reviews",
    # "transaction_sold_count":        "total_sold",
    # "listing_active_count":          "listing_active_count",
    # "main_category":                 "category",
    # "sub_category":                  "sub_category",
    # "listing_type":                  "listing_type",
    # "shipping_from_country_iso":     "ship_from",
    # "cached_visibility_score":       "visibility_score",
    # "cached_est_reviews":            "est_reviews",
    # "when_made":                     "when_made",
    # "who_made":                      "who_made",
    # "is_customizable":               "is_customizable",
    # "has_variations":                "has_variations",
    # # Locked
    # "est_mo_sales":                  "mo_sales",
    # "est_mo_revenue":                "mo_revenue",
    # "est_total_sales":               "total_sales_est",
    # "growth_rate":                   "growth_rate",
}

# ── Column order — 2 sheet: shop_summary và shop_listings ────────────────────
SHOP_COLUMN_ORDER = [
    "shop_name", "shop_id", "shop_url",
    "shop_review_count", "shop_review_avg", "shop_review_rate",
    "shop_total_sold", "shop_age_month",
    "listing_active_count", "shop_favorites", "shop_views",
    "shop_revenue", "shop_revenue_30d", "shop_sales_30d",
    "shop_conversion_rate", "shop_avg_listing_price", "shop_sale_per_listing",
    "ship_from", "shop_type", "shop_category",
    "shop_logo_url", "is_deactivated",
]

LISTING_COLUMN_ORDER = [
    "shop_name",
    "listing_id", "title", "price", "tags", "etsy_url", "image_url", 
    # "listing_age", "views", "favorites", "reviews", "avg_reviews", "state",
    # "total_sold", "visibility_score", "est_reviews",
    # "category", "sub_category", "listing_type",
    # "ship_from", "when_made", "who_made",
    # "is_customizable", "has_variations",
    # "mo_sales", "mo_revenue", "total_sales_est", "growth_rate",
]


# ── Custom exception ──────────────────────────────────────────────────────────
class _TokenExpired(Exception):
    pass


# ── build_session ─────────────────────────────────────────────────────────────
def build_session(auth_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Access-Token": auth_token,
        "Accept":         "application/json",
        "Content-Type":   "application/json",
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
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s


# ── get_token_via_browser ────────────────────────────────────────────────────
def get_token_via_browser() -> str:
    options = Options()
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.set_capability("ms:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Edge(options=options)
    driver.execute_cdp_cmd("Network.enable", {})
    driver.get("https://app.everbee.com/shop-analyzer")

    print(f"\n{ts()} {C.tag(C.ACTION, 'ACTION')} Browser đã mở.")
    print(f"{ts()} {C.tag(C.ACTION, 'ACTION')} Login EverBee + search bất kỳ keyword.")
    info(f"Token sẽ tự động lấy khi request API xuất hiện (timeout {TOKEN_TIMEOUT}s)...")

    token = ""
    elapsed = 0.0
    while elapsed < TOKEN_TIMEOUT:
        time.sleep(0.5)
        elapsed += 0.5
        try:
            logs = driver.get_log("performance")
        except Exception:
            continue
        for entry in logs:
            try:
                msg    = json.loads(entry.get("message", "{}"))
                method = msg.get("message", {}).get("method", "")
                if method != "Network.requestWillBeSent":
                    continue
                params  = msg.get("message", {}).get("params", {})
                req_url = params.get("request", {}).get("url", "")
                if "everbee." not in req_url:
                    continue
                headers   = params.get("request", {}).get("headers", {})
                token_val = headers.get("X-Access-Token") or headers.get("x-access-token", "")
                if token_val:
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


# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_flush(data: List[Dict], path: str = CHECKPOINT_CSV) -> None:
    if not data:
        return
    tmp = path + ".tmp"
    pd.DataFrame(data).to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Flush {len(data)} records → {path}")

def checkpoint_load(path: str = CHECKPOINT_CSV) -> Tuple[List[str], List[Dict], List[Dict]]:
    """Trả về (done_shops, shop_rows, listing_rows)."""
    if not os.path.exists(path):
        return [], [], []
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        done_shops   = df["shop_name"].dropna().unique().tolist() if "shop_name" in df.columns else []
        shop_rows    = df[df.get("listing_id", pd.Series(dtype=str)).isna() |
                         (df.get("listing_id", pd.Series(dtype=str)) == "")].to_dict("records") \
                         if "listing_id" in df.columns else df.to_dict("records")
        listing_rows = df[df.get("listing_id", pd.Series(dtype=str)).notna() &
                         (df.get("listing_id", pd.Series(dtype=str)) != "")].to_dict("records") \
                         if "listing_id" in df.columns else []
        ckpt(f"Loaded checkpoint: {len(done_shops)} shops done.")
        return done_shops, shop_rows, listing_rows
    except Exception as e:
        warn(f"Không đọc được checkpoint: {e}")
        return [], [], []

def checkpoint_setup_signal(shop_rows: List[Dict], listing_rows: List[Dict]) -> None:
    def _handler(sig, frame):
        stop("Ctrl+C — flush checkpoint rồi thoát...")
        checkpoint_flush(shop_rows,    CHECKPOINT_CSV.replace(".csv", "_shops.csv"))
        checkpoint_flush(listing_rows, CHECKPOINT_CSV.replace(".csv", "_listings.csv"))
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── parse helpers ─────────────────────────────────────────────────────────────
def clean_val(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s in LOCKED_VALUES else s

def parse_shop_details(shop: Dict) -> Dict:
    row: Dict = {}
    for api_field, col in SHOP_FIELDS.items():
        row[col] = clean_val(shop.get(api_field, ""))
    return row

def parse_listing(item: Dict, shop_name: str) -> Dict:
    row: Dict = {"shop_name": shop_name}
    for api_field, col in LISTING_FIELDS.items():
        val = item.get(api_field, "")
        # Images field là string URL trực tiếp
        if api_field == "Images":
            row[col] = clean_val(val) if isinstance(val, str) else ""
        else:
            row[col] = clean_val(val)
    # tags: array → string
    tags = item.get("tags", [])
    row["tags"] = ", ".join(t.strip() for t in tags if t) if isinstance(tags, list) else clean_val(tags)
    return row


# ── API fetch ─────────────────────────────────────────────────────────────────
def fetch_shop_page(
    session: requests.Session,
    shop_name: str,
    page: int,
) -> Tuple[Optional[Dict], List[Dict], int]:
    """
    Fetch 1 trang của shop.
    Trả về (shop_details, listings, total_pages).
    shop_details chỉ có ở page 1, None ở các trang sau.
    """
    params = {
        **DEFAULT_PARAMS,
        "shop_name": shop_name,
        "page": page,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(API_BASE, params=params, timeout=30)

            if r.status_code == 401:
                warn("Token hết hạn (401).")
                raise _TokenExpired()

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 10 * attempt))
                warn(f"Rate limit → ngủ {wait:.0f}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if r.status_code == 500:
                warn(f"500 Server Error tại page {page} shop '{shop_name}' — dừng shop này.")
                return None, [], -1

            if r.status_code == 404:
                warn(f"Shop '{shop_name}' không tìm thấy (404).")
                return None, [], 0

            r.raise_for_status()
            data = r.json()

            shop_details = data.get("shop_details") if page == 1 else None
            listings     = data.get("results", [])
            total_pages  = int(data.get("total_pages", 1))
            return shop_details, listings, total_pages

        except _TokenExpired:
            raise
        except requests.exceptions.ConnectionError as e:
            if "NameResolutionError" in str(e):
                err(f"Không resolve được host. Kiểm tra kết nối internet.")
                raise
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Connection error → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Request error: {e} → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)

    err(f"Không fetch được page {page} shop '{shop_name}' sau {MAX_RETRIES} lần.")
    return None, [], 0


# ── scrape_shop ───────────────────────────────────────────────────────────────
def scrape_shop(
    session: requests.Session,
    shop_name: str,
) -> Tuple[Optional[Dict], List[Dict]]:
    """
    Crawl toàn bộ pages của 1 shop.
    Trả về (shop_row, listing_rows).
    """
    shop_row: Optional[Dict] = None
    listing_rows: List[Dict] = []
    page = 1

    shop_details, listings, total_pages = fetch_shop_page(session, shop_name, page)

    if total_pages == 0:
        return None, []
    if total_pages == -1:
        return None, []

    # Parse shop details từ page 1
    if shop_details:
        shop_row = parse_shop_details(shop_details)

    # Parse listings page 1
    for item in listings:
        listing_rows.append(parse_listing(item, shop_name))

    effective_pages = min(total_pages, 500)
    info(f"  Shop '{shop_name}': {total_pages} trang listings (crawl tối đa {effective_pages})")

    # Crawl các trang còn lại
    for page in range(2, effective_pages + 1):
        _, listings, sig = fetch_shop_page(session, shop_name, page)
        if sig == -1:
            warn(f"  Dừng sớm tại page {page}.")
            break
        if not listings:
            break
        for item in listings:
            listing_rows.append(parse_listing(item, shop_name))

        print(
            f"  {ts()} page {page:>4}/{effective_pages}"
            f"  listings {C.tag(C.INFO, str(len(listing_rows)))}"
        )
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.1))

    return shop_row, listing_rows


# ── crawl_to_dataframe (entry point) ─────────────────────────────────────────
def crawl_to_dataframe(
    shop_names: List[str],
    delay_between_pages: tuple = (0.5, 1),
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Crawl danh sách shops.
    Trả về (df_shops, df_listings) — 2 DataFrame riêng biệt.
    """
    # ── Lấy token ─────────────────────────────────────────────────────────────
    auth_token = AUTH_TOKEN
    if not auth_token:
        info("Chưa có token — tự động mở browser để lấy...")
        auth_token = get_token_via_browser()

    # ── Checkpoint resume ──────────────────────────────────────────────────────
    done_shops:   List[str]  = []
    all_shop_rows:    List[Dict] = []
    all_listing_rows: List[Dict] = []

    ckpt_shops    = CHECKPOINT_CSV.replace(".csv", "_shops.csv")
    ckpt_listings = CHECKPOINT_CSV.replace(".csv", "_listings.csv")

    if os.path.exists(ckpt_shops) or os.path.exists(ckpt_listings):
        ans = input(
            f"{ts()} {C.tag(C.CKPT, 'CKPT')} Tìm thấy checkpoint. Resume? (y/n): "
        ).strip().lower()
        if ans == "y":
            if os.path.exists(ckpt_shops):
                df_s = pd.read_csv(ckpt_shops, dtype=str).fillna("")
                all_shop_rows = df_s.to_dict("records")
                done_shops    = df_s["shop_name"].tolist() if "shop_name" in df_s.columns else []
            if os.path.exists(ckpt_listings):
                df_l = pd.read_csv(ckpt_listings, dtype=str).fillna("")
                all_listing_rows = df_l.to_dict("records")
            ckpt(f"Loaded: {len(done_shops)} shops, {len(all_listing_rows)} listings.")
        else:
            for f in [ckpt_shops, ckpt_listings]:
                if os.path.exists(f): os.remove(f)
            info("Bắt đầu mới — đã xoá checkpoint cũ.")

    checkpoint_setup_signal(all_shop_rows, all_listing_rows)
    session = build_session(auth_token)

    remaining = [s for s in shop_names if s not in done_shops]
    info(f"Tổng {len(shop_names)} shops — còn lại {len(remaining)} shops cần crawl.")

    # ── Crawl từng shop ────────────────────────────────────────────────────────
    shops_since_flush = 0
    for i, shop_name in enumerate(remaining, 1):
        info(f"[{i}/{len(remaining)}] Crawl shop: '{shop_name}'")

        for _token_attempt in range(1, 4):
            try:
                shop_row, listing_rows = scrape_shop(session, shop_name)
                break
            except _TokenExpired:
                if _token_attempt >= 3:
                    err("Token hết hạn 3 lần — dừng.")
                    checkpoint_flush(all_shop_rows,    ckpt_shops)
                    checkpoint_flush(all_listing_rows, ckpt_listings)
                    sys.exit(1)
                warn(f"Token hết hạn — refresh (lần {_token_attempt})...")
                auth_token = get_token_via_browser()
                session    = build_session(auth_token)

        if shop_row:
            all_shop_rows.append(shop_row)
        all_listing_rows.extend(listing_rows)
        done_shops.append(shop_name)
        shops_since_flush += 1

        done(f"  '{shop_name}': {len(listing_rows)} listings")

        if shops_since_flush >= CHECKPOINT_EVERY:
            checkpoint_flush(all_shop_rows,    ckpt_shops)
            checkpoint_flush(all_listing_rows, ckpt_listings)
            shops_since_flush = 0

        smart_sleep(*delay_between_pages)

    # ── Final flush ────────────────────────────────────────────────────────────
    checkpoint_flush(all_shop_rows,    ckpt_shops)
    checkpoint_flush(all_listing_rows, ckpt_listings)

    # ── Build DataFrames ───────────────────────────────────────────────────────
    df_shops = pd.DataFrame(all_shop_rows)
    if not df_shops.empty:
        cols  = [c for c in SHOP_COLUMN_ORDER    if c in df_shops.columns]
        extra = [c for c in df_shops.columns     if c not in cols]
        df_shops = df_shops[cols + extra]

    df_listings = pd.DataFrame(all_listing_rows)
    if not df_listings.empty:
        cols  = [c for c in LISTING_COLUMN_ORDER if c in df_listings.columns]
        extra = [c for c in df_listings.columns  if c not in cols]
        df_listings = df_listings[cols + extra]

    # ── Lưu output ────────────────────────────────────────────────────────────
    # shops_csv    = OUTPUT_CSV.replace(".csv",  "_shops.csv")
    listings_csv = OUTPUT_CSV.replace(".csv",  "_listings.csv")
    # shops_xlsx   = OUTPUT_XLSX.replace(".xlsx", "_shops.xlsx")
    listings_xlsx= OUTPUT_XLSX.replace(".xlsx", "_listings.xlsx")

    #df_shops.to_csv(shops_csv,       index=False, encoding="utf-8-sig")
    df_listings.to_csv(listings_csv, index=False, encoding="utf-8-sig")
    #done(f"CSV saved → {shops_csv} ({len(df_shops)} shops)")
    done(f"CSV saved → {listings_csv} ({len(df_listings)} listings)")

    #df_shops.to_excel(shops_xlsx,       index=False)
    df_listings.to_excel(listings_xlsx, index=False)
    #done(f"Excel saved → {shops_xlsx}")
    done(f"Excel saved → {listings_xlsx}")

    # Xoá checkpoint
    for f in [ckpt_shops, ckpt_listings]:
        if os.path.exists(f):
            os.remove(f)
    ckpt("Checkpoint đã xoá.")

    return df_shops, df_listings


def smart_sleep(min_s: float = 0.5, max_s: float = 1.0):
    time.sleep(random.uniform(min_s, max_s))


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    SHOP_NAMES = [
        "GONdesignJEWELRY",
    #     "AutumnloveByDaniel",
    #     "TrueFlowInk",
    #     "PhotoOnShirt",
    ]

    df_shops, df_listings = crawl_to_dataframe(
        shop_names=SHOP_NAMES,
        delay_between_pages=(0.5, 1),
    )

    if not df_shops.empty:
        print("\n── Shop Summary ──")
        print(df_shops.to_string(index=False))

    if not df_listings.empty:
        print(f"\n── Listings ({len(df_listings)} rows, preview 5) ──")
        print(df_listings.head().to_string(index=False))