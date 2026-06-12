"""
Etsy Shop Review Crawler — Selenium
=====================================
Dùng Chrome thật nên không bị 403/bot detection.

Cài:
    pip install selenium beautifulsoup4

Chạy:
    python etsy_review_selenium.py --shop TênShop
    python etsy_review_selenium.py --shop TênShop --output reviews.csv --headless
"""

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
# from selenium import webdriver
# from selenium.webdriver.chrome.options import Options
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException


# ─── CONFIG ──────────────────────────────────────────────────────────────────

DEFAULT_OUTPUT  = "etsy_reviews.csv"
PAGE_WAIT_SEC   = 2.5    # chờ sau khi click Next
ELEMENT_TIMEOUT = 10     # timeout chờ element xuất hiện (giây)
MAX_PAGES       = 500    # giới hạn an toàn


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def parse_date(raw: str) -> str:
    """'Jun 9, 2026' → '09-06-2026'"""
    if not raw:
        return ""
    cleaned = re.sub(r"^on\s+", "", raw.strip(), flags=re.I)
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return cleaned


def parse_rating(raw: str) -> str:
    """'5 out of 5 stars' → '5'"""
    m = re.match(r"(\d)", raw.strip())
    return m.group(1) if m else raw.strip()


def clean_url(href: str) -> str:
    base = href.split("?")[0].split("#")[0]
    return base if base.startswith("http") else "https://www.etsy.com" + base


# ─── PARSE PAGE ──────────────────────────────────────────────────────────────

def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    reviews = []

    for block in soup.select('li[data-region="review"]'):
        # name
        name_el = block.select_one(".shop2-review-attribution a")
        if name_el:
            name = name_el.get_text(strip=True)
        else:
            # fallback: "Etsy buyer on Jun 9, 2026" — không có <a>
            attr_text = block.select_one(".shop2-review-attribution")
            full = attr_text.get_text(strip=True) if attr_text else ""
            on_idx = full.find(" on ")
            name = full[:on_idx].strip() if on_idx != -1 else full

        # date
        attr_el = block.select_one(".shop2-review-attribution")
        date_raw = ""
        if attr_el:
            full = attr_el.get_text(strip=True)
            on_idx = full.find(" on ")
            date_raw = full[on_idx + 4:].strip() if on_idx != -1 else ""
        date = parse_date(date_raw)

        # rating — ưu tiên hidden input (chính xác nhất)
        rating = ""
        rating_input = block.select_one('input[name="rating"]')
        if rating_input:
            rating = rating_input.get("value", "")
        else:
            rating_el = block.select_one(".stars-svg .screen-reader-only")
            if rating_el:
                rating = parse_rating(rating_el.get_text())

        # item url & name
        item_url, item_name = "", ""
        link_el = block.select_one('[data-region="listing"] a[href*="/listing/"]')
        if link_el:
            item_url  = clean_url(link_el.get("href", ""))
            item_name = (
                link_el.get("aria-label")
                or link_el.get_text(strip=True)
            )

        if name or item_url:
            reviews.append({
                "name":      name,
                "date":      date,
                "rating":    rating,
                "item_url":  item_url,
                "item_name": item_name,
            })

    return reviews


# ─── DRIVER SETUP ────────────────────────────────────────────────────────────
def build_driver(headless: bool) -> uc.Chrome:
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    
    driver = uc.Chrome(options=options)
    return driver

# def build_driver(headless: bool) -> webdriver.Chrome:
#     options = Options()

#     if headless:
#         options.add_argument("--headless=new")

#     # Giảm dấu hiệu automation
#     options.add_argument("--disable-extensions")
#     options.add_argument("--disable-infobars")
#     options.add_argument("--disable-blink-features=AutomationControlled")
#     options.add_experimental_option("excludeSwitches", ["enable-automation"])
#     options.add_experimental_option("useAutomationExtension", False)
#     options.add_argument("--no-sandbox")
#     options.add_argument("--disable-dev-shm-usage")
#     options.add_argument("--window-size=1280,900")
#     options.add_argument(
#         "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#         "AppleWebKit/537.36 (KHTML, like Gecko) "
#         "Chrome/124.0.0.0 Safari/537.36"
#     )

#     driver = webdriver.Chrome(options=options)

#     # Xóa property webdriver để bypass detection
#     driver.execute_script(
#         "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
#     )

#     return driver


# ─── NEXT PAGE ───────────────────────────────────────────────────────────────

def get_next_url(driver) -> str | None:
    """
    Lấy href của nút Next page từ pagination.
    Dùng href thay vì click để tránh stale element.
    """
    try:
        # Tìm nút Next (arrow cuối pagination) có href hợp lệ
        next_links = driver.find_elements(
            By.CSS_SELECTOR,
            'a[href*="reviews?ref=pagination"][data-page]'
        )
        # Lấy page hiện tại
        current = driver.find_element(
            By.CSS_SELECTOR,
            'a[aria-current="true"][href*="page="]'
        )
        current_page = int(re.search(r"page=(\d+)", current.get_attribute("href")).group(1))

        for link in next_links:
            href = link.get_attribute("href")
            m = re.search(r"page=(\d+)", href)
            if m and int(m.group(1)) == current_page + 1:
                return href

    except (NoSuchElementException, AttributeError, TypeError):
        pass

    # Fallback: tìm nút mũi tên Next không disabled
    try:
        next_arrow = driver.find_element(
            By.CSS_SELECTOR,
            'a.wt-btn--icon[href*="reviews?ref=pagination"]:not(.wt-is-disabled)'
        )
        href = next_arrow.get_attribute("href")
        if href and "page=" in href:
            return href
    except NoSuchElementException:
        pass

    return None


# ─── MAIN ────────────────────────────────────────────────────────────────────

def crawl(shop: str, output: str, headless: bool) -> None:
    start_url = f"https://www.etsy.com/shop/{shop}/reviews"
    print(f"[Crawler] Shop    : {shop}")
    print(f"[Crawler] URL     : {start_url}")
    print(f"[Crawler] Output  : {output}")
    print(f"[Crawler] Headless: {headless}")
    print()

    all_reviews: list[dict] = []
    seen: set[tuple]        = set()
    first_item_url: str | None = None

    driver = build_driver(headless)
    wait   = WebDriverWait(driver, ELEMENT_TIMEOUT)

    try:
        driver.get(start_url)

        for page_num in range(1, MAX_PAGES + 1):
            print(f"─── Page {page_num} ───")

            # Đợi review block load
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'li[data-region="review"]')
                ))
            except TimeoutException:
                print("  [warn] Timeout — không tìm thấy review block.")
                break

            # Parse
            reviews = parse_page(driver.page_source)
            print(f"  Tìm thấy {len(reviews)} reviews")

            if not reviews:
                print("  Không có review — dừng.")
                break

            # Lưu & check điều kiện dừng
            stop = False
            for r in reviews:
                if first_item_url is None and r["item_url"]:
                    first_item_url = r["item_url"]
                    print(f"  [first_url] {first_item_url}")

                if all_reviews and r["item_url"] and r["item_url"] == first_item_url:
                    print("  [STOP] Gặp lại listing đầu tiên.")
                    stop = True
                    break

                key = (r["name"], r["date"], r["item_url"])
                if key not in seen:
                    seen.add(key)
                    all_reviews.append(r)

            if stop:
                break

            # Tìm URL trang tiếp theo
            next_url = get_next_url(driver)
            if not next_url:
                print("  [STOP] Không còn trang tiếp theo.")
                break

            print(f"  → Chuyển sang: {next_url}")
            driver.get(next_url)
            time.sleep(PAGE_WAIT_SEC)

    finally:
        driver.quit()

    # ─── EXPORT CSV ──────────────────────────────────────────────────────────
    print(f"\n[Crawler] Tổng: {len(all_reviews)} reviews → {output}")

    fields = ["name", "date", "rating", "item_url", "item_name"]
    with Path(output).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_reviews)

    print(f"[Crawler] ✓ Đã lưu: {Path(output).resolve()}")

    # Preview
    print("\n── Preview 5 dòng đầu ──")
    for r in all_reviews[:5]:
        print(r)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crawl Etsy shop reviews → CSV (Selenium)")
    parser.add_argument("--shop",     required=True, help="Tên shop Etsy")
    parser.add_argument("--output",   default=DEFAULT_OUTPUT, help="File CSV output")
    parser.add_argument("--headless", action="store_true",    help="Ẩn cửa sổ browser")
    args = parser.parse_args()
    crawl(args.shop, args.output, args.headless)


if __name__ == "__main__":
    main()