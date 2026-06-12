import pandas as pd
import glob
import re
import os

INPUT_PATTERN = "etsy-com-*.csv"
OUTPUT_FILE = "etsy_review.csv"


def extract_listing_id(url: str) -> str:
    if not isinstance(url, str):
        return ""
    match = re.search(r"/listing/(\d+)", url)
    return match.group(1) if match else ""


def extract_date(data: str) -> str:
    """Extract date từ 'Berlin on Oct 9, 2025' -> '2025-10-09'"""
    if not isinstance(data, str):
        return ""
    match = re.search(r"on\s+(\w+ \d{1,2},\s*\d{4})", data)
    if match:
        try:
            return pd.to_datetime(match.group(1)).strftime("%Y-%m-%d")
        except Exception:
            return match.group(1)
    return ""


def merge_csv_files(pattern: str = INPUT_PATTERN, output: str = OUTPUT_FILE) -> None:
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"Không tìm thấy file nào khớp với pattern: {pattern}")
        return

    print(f"Tìm thấy {len(files)} file: {files}")

    dfs = []
    for f in files:
        df = pd.read_csv(f, dtype=str)
        df["_source_file"] = os.path.basename(f)
        dfs.append(df)

    merged = pd.concat(dfs, ignore_index=True)
    print(f"Tổng số dòng trước dedup: {len(merged)}")

    # Tách review_date trước để dùng làm tiêu chí dedup
    if "data" in merged.columns:
        merged["review_date"] = merged["data"].apply(extract_date)

    # Đếm số lần mỗi url xuất hiện (= số review thu thập được)
    review_counts = merged.groupby("url", sort=False).size().rename("review_count")
    merged = merged.join(review_counts, on="url")

    # Dedup theo url: giữ dòng có review_date mới nhất
    merged["_date_sort"] = pd.to_datetime(merged["review_date"], errors="coerce")
    merged = (
        merged
        .sort_values("_date_sort", ascending=False, na_position="last")
        .drop_duplicates(subset=["url"], keep="first")
        .drop(columns=["_date_sort"])
        .sort_index()
    )
    print(f"Tổng số dòng sau dedup: {len(merged)}")

    # Sắp xếp lại cột: listing_id sau url, review_date & review_count sau data
    merged.insert(merged.columns.get_loc("url") + 1, "listing_id", merged["url"].apply(extract_listing_id))
    if "review_date" in merged.columns:
        col = merged.pop("review_date")
        merged.insert(merged.columns.get_loc("data") + 1, "review_date", col)
    if "review_count" in merged.columns:
        col = merged.pop("review_count")
        merged.insert(merged.columns.get_loc("review_date") + 1, "review_count", col)

    merged = merged.drop(columns=["_source_file"])
    merged.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"Đã lưu: {output}")


if __name__ == "__main__":
    merge_csv_files()