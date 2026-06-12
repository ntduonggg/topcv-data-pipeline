import time
import re
import os
import sys
import signal
import glob
import shutil
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ── ANSI color logging ────────────────────────────────────────────────────────
class C:
    INFO      = "\033[94m"
    ACTION    = "\033[93m"
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

def info(msg):   print(f"{ts()} {C.tag(C.INFO,      'INFO')}   {msg}")
def action(msg): print(f"{ts()} {C.tag(C.ACTION,    'ACTION')} {msg}")
def warn(msg):   print(f"{ts()} {C.tag(C.WARN,      'WARN')}   {msg}")
def ckpt(msg):   print(f"{ts()} {C.tag(C.CKPT,      'CKPT')}   {msg}")
def err(msg):    print(f"{ts()} {C.tag(C.ERROR,      'ERROR')}  {msg}")
def done(msg):   print(f"{ts()} {C.tag(C.DONE,      'DONE')}   {msg}")
def stop(msg):   print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}   {msg}")


# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV        = "hidden_listings.csv"  # CSV từ everbee_shop_scraper (có etsy_url)
OUTPUT_CSV       = "heyetsy_image_urls.csv"              # Output: thêm cột image_1, image_2, ...
BULK_URL         = "https://heyetsy.com/tools/bulk-etsy-images-downloader"
ETSY_LISTING_URL = "https://www.etsy.com/listing/{id}"

BATCH_SIZE       = 40     # max 50 URLs/lần theo giới hạn trang
CAPTCHA_TIMEOUT  = 120    # giây chờ user tick CAPTCHA
RESULT_TIMEOUT   = 60     # giây chờ kết quả load sau khi Pull
DOWNLOAD_DIR     = os.path.join(os.path.expanduser("~"), "Downloads")
DOWNLOAD_WAIT    = 15     # giây chờ file CSV download xong
SAVE_EVERY       = 5      # lưu OUTPUT_CSV sau mỗi N batch

# Retry config (tránh 429 / 502)
BATCH_MAX_RETRY    = 3    # số lần retry mỗi batch khi gặp lỗi
RETRY_BACKOFF      = 30   # giây nghỉ cơ bản khi 429 (x attempt → exponential)
BAD_GATEWAY_BACKOFF = 60  # giây nghỉ khi gặp 502 Bad Gateway (server cần hồi phục lâu hơn)
BETWEEN_BATCH      = 5    # giây nghỉ bình thường giữa các batch

# Chỉ match các chuỗi đặc trưng của trang lỗi 429 thực sự
# Không dùng "rate limit" / "slow down" vì có thể xuất hiện trong nội dung trang bình thường
RATE_LIMIT_SIGNALS = [
    "429 Too Many Requests",
    "HTTP 429",
    "<title>429",
    "too many requests</",   # thường trong <h1> hoặc <p> của trang lỗi
]


# ── build_driver ──────────────────────────────────────────────────────────────
def build_driver(download_dir: str = DOWNLOAD_DIR) -> webdriver.Edge:
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    )
    # Tự động lưu file download vào thư mục chỉ định
    prefs = {
        "download.default_directory":   download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade":   True,
    }
    options.add_experimental_option("prefs", prefs)
    return webdriver.Edge(options=options)


# ── is_rate_limited ───────────────────────────────────────────────────────────
def is_rate_limited(driver: webdriver.Edge) -> bool:
    """
    Phát hiện 429 thực sự — không bị false positive từ nội dung trang bình thường.
    Ưu tiên check title trang trước (nhanh, chính xác), sau đó mới scan body.
    """
    try:
        title = driver.title.lower()
        if "429" in title or "too many requests" in title:
            return True
    except Exception:
        pass

    src = driver.page_source
    return any(s in src for s in RATE_LIMIT_SIGNALS)


# ── wait_for_login ────────────────────────────────────────────────────────────
def wait_for_login(driver: webdriver.Edge, timeout: int = 180) -> bool:
    driver.get("https://heyetsy.com/login")
    print()
    action("Browser đã mở trang login HeyEtsy.")
    action("Vui lòng đăng nhập vào tài khoản HeyEtsy.")
    info(f"Chờ login tối đa {timeout}s...")
    for _ in range(timeout):
        time.sleep(1)
        if "/login" not in driver.current_url and "heyetsy.com" in driver.current_url:
            done("Login thành công!")
            return True
    err("Timeout — không phát hiện login.")
    return False


# ── load_input_csv ────────────────────────────────────────────────────────────
def load_input_csv(csv_path: str) -> pd.DataFrame:
    """
    Đọc CSV đầu vào. Cần có cột: listing_id, shop_name, title, tags.
    Nếu đã có cột image_1 thì giữ nguyên để merge sau.
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    if "listing_id" not in df.columns and "etsy_url" not in df.columns:
        raise ValueError(f"CSV cần có cột 'listing_id' hoặc 'etsy_url': {csv_path}")
    # Nếu không có listing_id, extract từ etsy_url
    if "listing_id" not in df.columns and "etsy_url" in df.columns:
        df["listing_id"] = df["etsy_url"].str.extract(r"/listing/(\d+)")[0].fillna("")
    info(f"Loaded {len(df)} rows từ {csv_path}")
    return df


# ── get_missing_indices ───────────────────────────────────────────────────────
def get_missing_indices(df: pd.DataFrame) -> List[int]:
    """Trả về list index của các dòng chưa có image_1."""
    # if "image_1" not in df.columns:
    return list(df.index)
    # return list(df[df["image_1"] == ""].index)


# ── chunk_batches ─────────────────────────────────────────────────────────────
def chunk_batches(indices: List[int], size: int = BATCH_SIZE) -> List[List[int]]:
    return [indices[i:i+size] for i in range(0, len(indices), size)]


# ── build_etsy_urls ───────────────────────────────────────────────────────────
def build_etsy_urls(df: pd.DataFrame, indices: List[int]) -> List[str]:
    """
    Ưu tiên dùng cột etsy_url nếu có (từ everbee_shop_data_listings.csv).
    Fallback: ghép từ listing_id.
    """
    has_etsy_url = "etsy_url" in df.columns
    urls = []
    for idx in indices:
        if has_etsy_url and df.at[idx, "etsy_url"]:
            urls.append(df.at[idx, "etsy_url"])
        else:
            urls.append(ETSY_LISTING_URL.format(id=df.at[idx, "listing_id"]))
    return urls


# ── is_bad_gateway ───────────────────────────────────────────────────────────
def is_bad_gateway(driver: webdriver.Edge) -> bool:
    """Phát hiện 502 Bad Gateway — server HeyEtsy tạm thời không phản hồi."""
    try:
        title = driver.title.lower()
        if "502" in title or "bad gateway" in title:
            return True
    except Exception:
        pass
    src = driver.page_source
    return "502 Bad Gateway" in src or "Bad Gateway" in src


# ── fill_textarea_and_pull ────────────────────────────────────────────────────
def fill_textarea_and_pull(
    driver: webdriver.Edge,
    etsy_urls: List[str],
) -> bool:
    """
    1. Mở bulk tool
    2. Điền URLs vào textarea
    3. In thông báo terminal → chờ user tick CAPTCHA + tự nhấn Pull
    4. Phát hiện Pull đã được nhấn khi table thay đổi (không còn "No listings found")
    5. Chờ kết quả load xong
    """
    driver.get(BULK_URL)
    time.sleep(2)

    # Kiểm tra 502 Bad Gateway ngay khi load trang
    if is_bad_gateway(driver):
        err("502 Bad Gateway khi load bulk tool — trả về False để retry.")
        return False

    # Điền URLs vào textarea
    try:
        textarea = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "textarea#listings"))
        )
    except Exception:
        try:
            textarea = driver.find_element(By.TAG_NAME, "textarea")
        except Exception:
            err("Không tìm thấy textarea trên trang bulk tool.")
            return False

    textarea.clear()
    textarea.send_keys("\n".join(etsy_urls))
    info(f"Đã điền {len(etsy_urls)} URLs vào textarea.")

    # Hướng dẫn user
    print()
    action("=" * 55)
    action("  1. Tick 'I\'m not a robot' trên trình duyệt")
    action("  2. Nhấn nút 'Pull Etsy Listings'")
    action("  Script sẽ tự động tiếp tục sau khi kết quả load.")
    action("=" * 55)
    print()

    # Chờ user nhấn Pull — phát hiện khi table không còn "No listings found"
    # hoặc khi table biến mất (đang load) rồi xuất hiện lại
    info(f"Đang chờ kết quả... (timeout {RESULT_TIMEOUT}s)")
    try:
        WebDriverWait(driver, RESULT_TIMEOUT).until_not(
            EC.text_to_be_present_in_element(
                (By.CSS_SELECTOR, "table"), "No listings found"
            )
        )
        done("Kết quả đã load xong.")
    except Exception:
        warn("Timeout chờ kết quả — có thể trang load chậm, thử export luôn.")

    return True


# ── click_export_and_get_csv ──────────────────────────────────────────────────
def click_export_and_get_csv(driver: webdriver.Edge) -> str | None:
    """
    Click 'Export All Images' → chờ file CSV download → trả về đường dẫn file.
    """
    # Lấy danh sách file CSV hiện có trong Downloads trước khi click
    before = set(glob.glob(os.path.join(DOWNLOAD_DIR, "*.csv")))

    try:
        export_btn = driver.find_element(
            By.XPATH,
            "//button[contains(., 'Export All Images') or contains(., 'Export All')]"
            "[not(contains(., 'Video'))]"
        )
        export_btn.click()
        info("Đã click 'Export All Images' — chờ download...")
    except Exception:
        err("Không tìm thấy nút 'Export All Images'.")
        return None

    # Chờ file mới xuất hiện trong Downloads
    for _ in range(DOWNLOAD_WAIT):
        time.sleep(1)
        after = set(glob.glob(os.path.join(DOWNLOAD_DIR, "*.csv")))
        new_files = after - before
        if new_files:
            # Lấy file mới nhất
            csv_path = max(new_files, key=os.path.getmtime)
            done(f"Download xong → {csv_path}")
            return csv_path

    err(f"Không tìm thấy file CSV mới sau {DOWNLOAD_WAIT}s.")
    return None


# ── URL regex (compile 1 lần) ─────────────────────────────────────────────────
_URL_REGEX = re.compile(
    r"https://i\.etsystatic\.com/\S+?il_fullxfull\.\S+?\.jpg"
)


# ── parse_exported_csv ────────────────────────────────────────────────────────
def parse_exported_csv(csv_path: str) -> Dict[str, List[str]]:
    """
    Parse CSV export của HeyEtsy bulk tool.

    KHÔNG dùng pd.read_csv vì:
    - URL chứa dấu phẩy → pandas split sai cột
    - Pandas lấy dòng đầu làm chuẩn số cột → bỏ sót dòng có nhiều ảnh hơn

    Thay bằng đọc từng dòng raw:
    - Tách listing_id tại dấu phẩy đầu tiên
    - Regex extract toàn bộ URL il_fullxfull từ phần còn lại
    """
    result: Dict[str, List[str]] = {}

    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        err(f"Không đọc được CSV export: {e}")
        return result

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Tách listing_id tại dấu phẩy đầu tiên
        comma_pos = line.find(",")
        if comma_pos == -1:
            continue

        first = line[:comma_pos].strip()

        # Nếu cột đầu là etsy URL thì extract id
        m = re.search(r"/listing/(\d+)", first)
        listing_id = m.group(1) if m else first

        # Bỏ qua nếu không phải số thuần (header, dòng lỗi, ...)
        if not listing_id.isdigit():
            continue

        # Extract toàn bộ URL fullxfull từ phần còn lại
        rest = line[comma_pos + 1:]
        raw_urls = _URL_REGEX.findall(rest)

        # Dedup giữ thứ tự
        seen: set = set()
        urls: List[str] = []
        for url in raw_urls:
            url = url.rstrip(".,")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        if not urls:
            continue

        if listing_id in result:
            for url in urls:
                if url not in set(result[listing_id]):
                    result[listing_id].append(url)
        else:
            result[listing_id] = urls

    return result


# ── merge_images_into_df ──────────────────────────────────────────────────────
def merge_images_into_df(
    df: pd.DataFrame,
    image_map: Dict[str, List[str]],
) -> int:
    """
    Ghi image URLs từ image_map vào đúng dòng trong df.
    Trả về số dòng đã được cập nhật.
    """
    updated = 0
    for listing_id, urls in image_map.items():
        if not urls:
            continue
        matches = df[df["listing_id"] == listing_id].index
        if matches.empty:
            continue
        idx = matches[0]

        # Xoá image columns cũ của dòng này
        for c in df.columns:
            if re.match(r"^image_\d+$", c):
                df.at[idx, c] = ""

        # Ghi URLs mới
        for n, url in enumerate(urls, 1):
            col = f"image_{n}"
            if col not in df.columns:
                df[col] = ""
            df.at[idx, col] = url

        updated += 1

    return updated


# ── save_df ───────────────────────────────────────────────────────────────────
def save_df(df: pd.DataFrame, path: str) -> None:
    # Chỉ giữ: shop_name, listing_id, title, tags, image_1, image_2, ...
    fixed  = [c for c in ["shop_name", "listing_id", "title", "tags"] if c in df.columns]
    images = sorted(
        [c for c in df.columns if re.match(r"^image_\d+$", c)],
        key=lambda x: int(x.split("_")[1])
    )
    df     = df[fixed + images].fillna("")
    tmp    = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    ckpt(f"Saved → {path}")


# ── run ───────────────────────────────────────────────────────────────────────
def run(
    input_csv:  str = INPUT_CSV,
    output_csv: str = OUTPUT_CSV,
) -> pd.DataFrame:
    """
    Entry point chính.

    Flow mỗi batch (50 listings):
      1. Build Etsy URLs từ listing_id
      2. Mở bulk tool → điền textarea → chờ user tick CAPTCHA → Pull
      3. Export All Images → download CSV
      4. Parse CSV → merge vào DataFrame
      5. Lưu định kỳ
    """
    df = load_input_csv(input_csv)
    missing = get_missing_indices(df)
    # info(f"Dòng thiếu ảnh: {len(missing)} / {len(df)}")

    # if not missing:
    #     done("Không có dòng nào thiếu ảnh — không cần làm gì.")
    #     return df

    batches = chunk_batches(missing, BATCH_SIZE)
    info(f"Chia thành {len(batches)} batch (mỗi batch tối đa {BATCH_SIZE} listings)")

    # Signal handler
    def _handler(sig, frame):
        stop("Ctrl+C — lưu CSV trước khi thoát...")
        save_df(df, output_csv)
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)

    driver = build_driver()

    try:
        if not wait_for_login(driver):
            err("Login thất bại — thoát.")
            sys.exit(1)

        total_updated   = 0
        batches_done    = 0

        for b_idx, batch_indices in enumerate(batches, 1):
            etsy_urls = build_etsy_urls(df, batch_indices)

            print(f"\n{'═'*60}")
            info(f"BATCH {b_idx}/{len(batches)} — {len(etsy_urls)} listings")
            print(f"{'═'*60}")

            # Retry loop cho mỗi batch
            batch_success = False
            for attempt in range(1, BATCH_MAX_RETRY + 1):
                if attempt > 1:
                    # Phân biệt 502 vs 429 để backoff phù hợp
                    if is_bad_gateway(driver):
                        backoff = BAD_GATEWAY_BACKOFF
                        warn(f"502 Bad Gateway — nghỉ {backoff}s cho server hồi phục...")
                    else:
                        backoff = RETRY_BACKOFF * (attempt - 1)
                        warn(f"Retry batch {b_idx} lần {attempt}/{BATCH_MAX_RETRY} — nghỉ {backoff}s...")
                    time.sleep(backoff)

                # Fill textarea + hướng dẫn user Pull
                ok = fill_textarea_and_pull(driver, etsy_urls)
                if not ok:
                    # Có thể là 502 xảy ra khi load trang
                    if is_bad_gateway(driver):
                        warn(f"502 Bad Gateway khi load trang (attempt {attempt}) — sẽ retry.")
                    else:
                        warn(f"fill_textarea_and_pull thất bại (attempt {attempt}) — sẽ retry.")
                    continue

                # Kiểm tra 429 sau khi Pull
                if is_rate_limited(driver):
                    warn(f"429 detected sau Pull (attempt {attempt}) — sẽ retry.")
                    continue

                # Export & download CSV
                csv_file = click_export_and_get_csv(driver)
                if not csv_file:
                    warn(f"Không download được CSV (attempt {attempt}) — sẽ retry.")
                    continue

                # Parse CSV export
                image_map = parse_exported_csv(csv_file)

                # Xoá file CSV tạm
                try:
                    os.remove(csv_file)
                except Exception:
                    pass

                if not image_map:
                    warn(f"CSV parse rỗng (attempt {attempt}) — sẽ retry.")
                    continue

                # Merge vào DataFrame
                info(f"Parse được {len(image_map)} listings có ảnh từ CSV export.")
                updated = merge_images_into_df(df, image_map)
                total_updated += updated
                done(f"Batch {b_idx}: cập nhật {updated} dòng.")
                batch_success = True
                break

            if not batch_success:
                warn(f"Batch {b_idx} thất bại sau {BATCH_MAX_RETRY} lần thử — bỏ qua.")
                continue

            batches_done += 1

            # Lưu định kỳ
            if batches_done % SAVE_EVERY == 0:
                save_df(df, output_csv)

            # Nghỉ giữa các batch
            if b_idx < len(batches):
                info(f"Nghỉ {BETWEEN_BATCH}s trước batch tiếp theo...")
                time.sleep(BETWEEN_BATCH)

    finally:
        driver.quit()
        info("Browser đã đóng.")

    # Lưu lần cuối
    save_df(df, output_csv)

    # Summary
    still_missing = len(get_missing_indices(df))
    print(f"\n{'─'*50}")
    done("Hoàn tất!")
    info(f"  Tổng dòng cập nhật : {total_updated}")
    info(f"  Vẫn còn thiếu ảnh  : {still_missing}")
    info(f"  Output             : {output_csv}")
    print(f"{'─'*50}\n")

    return df


# ── __main__ ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run(
        input_csv=INPUT_CSV,
        output_csv=OUTPUT_CSV,
    )