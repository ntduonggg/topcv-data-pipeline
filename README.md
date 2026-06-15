# Web Scrapers & Data Pipeline

A multi-project Python monorepo covering job-market data collection, Etsy product research, and mockup art extraction — with Docker-based ingestion and Trello integration.

---

## Projects

| Project | Folder | Description |
|---|---|---|
| TopCV Job Scraper | `job-scraper/` | Scrapes Data Engineer jobs from TopCV into PostgreSQL |
| HeyEtsy & Everbee Scraper | `product-scraper/` | Collects Etsy listing data, images, tags, and reviews |
| Mockup Generator | `mockup-generator/` | Extracts and processes artwork from product mockup images |

---

## Project 1 — TopCV Job Scraper & Data Pipeline

Scraping Data Engineer job listings from TopCV, cleaning the data, and ingesting it into PostgreSQL — fully containerised with Docker.

### Business Context

The goal is to collect Data Engineer job postings from TopCV (Vietnam's largest job platform), store them in a structured PostgreSQL database, and make them available for analysis. The pipeline handles scraping, data cleaning, encoding issues with Vietnamese text, and automated ingestion — all running inside Docker containers connected via a shared network.

### Repo Structure

```
job-scraper/
├── topcv_data_scraper.py       # scrapes job listings from TopCV
├── topcv_scraper.py            # scraper utilities
└── topcv_job_urls.csv          # collected job URLs
data-ingestion/
└── ingest_data.py              # cleans and ingests CSV into PostgreSQL
data-files/
├── topcv_data_jobs.csv         # raw scraped data
└── topcv_data_jobs.xlsx        # Excel version
Dockerfile                      # builds the pipeline image
pyproject.toml                  # dependencies managed by uv
```

### Data Collected

| Column | Description |
|---|---|
| `title` | Job title |
| `detail_title` | Full job title |
| `job_url` | Link to the job posting |
| `company` | Company short name |
| `company_name_full` | Full company name |
| `salary_list` | Salary range |
| `detail_location` | Work location |
| `detail_experience` | Years of experience required |
| `deadline` | Application deadline |
| `tags` | Job tags and benefits |
| `working_addresses` | Office address |
| `working_times` | Working hours |
| `desc_mota` | Job description |
| `desc_yeucau` | Job requirements |
| `desc_quyenloi` | Benefits |
| `company_website` | Company website |
| `company_size` | Number of employees |
| `company_industry` | Industry sector |
| `company_address` | Company address |
| `company_description` | About the company |

### Technical Challenges & Solutions

- **Vietnamese text encoding** — `charmap` codec errors when reading/writing Vietnamese characters. Solution: always use `encoding="utf-8"` for file I/O and `encoding="utf-8-sig"` for CSV exports.
- **Mixed date formats** — deadline field stored as string (`DD/MM/YYYY`). Solution: cast to `DATE` in PostgreSQL after ingestion using `TO_DATE(deadline, 'DD/MM/YYYY')`.
- **Docker networking** — pipeline container couldn't reach PostgreSQL using `localhost`. Solution: both containers run on the same Docker network (`pg-network`), and the host is referenced by container name (`pgdatabase`).
- **Port conflict** — local PostgreSQL installation occupied port 5432, blocking Docker. Solution: identified and killed the conflicting PID via `netstat` + `taskkill`.
- **psycopg vs psycopg2** — `psycopg` (v3) requires `libpq-dev` and `gcc` on slim images. Solution: added system dependencies in Dockerfile and used `postgresql+psycopg://` connection string.
- **Hardcoded credentials overriding CLI args** — click arguments were being ignored due to hardcoded values in the function body. Solution: removed hardcoded variables and relied solely on click-passed arguments.

### Tech Stack

- **Language:** Python 3.13
- **Scraping:** BeautifulSoup4, Requests
- **Data processing:** Pandas
- **Database:** PostgreSQL 18 (Docker)
- **ORM/Ingestion:** SQLAlchemy + psycopg (v3)
- **CLI:** Click
- **Package manager:** uv
- **Containerisation:** Docker

### How to Run

**Prerequisites:** Docker Desktop, uv (`pip install uv`)

```bash
# 1 — Create Docker network
docker network create pg-network

# 2 — Start PostgreSQL
docker run -it --rm \
  --network=pg-network \
  --name=pgdatabase \
  -e POSTGRES_USER="root" \
  -e POSTGRES_PASSWORD="root" \
  -e POSTGRES_DB="topcv_data" \
  -p 5432:5432 \
  postgres:18

# 3 — Build the pipeline image
docker build -t topcv_pipeline:v001 .

# 4 — Run the pipeline
docker run -it \
  --network=pg-network \
  topcv_pipeline:v001 \
  --pg-user=root \
  --pg-pass=root \
  --pg-host=pgdatabase \
  --pg-port=5432 \
  --pg-db=topcv_data \
  --target-table=data_engineer_job

# 5 — Verify
uv run pgcli postgresql://root:root@localhost:5432/topcv_data
```
```sql
SELECT * FROM data_engineer_job LIMIT 5;
```

### Key Learnings

- Always use `utf-8` encoding when working with Vietnamese text — `charmap` errors are silent killers on Windows.
- Docker containers communicate via container names on shared networks, not `localhost`.
- Check PIDs carefully before killing processes — killing Docker Desktop itself will bring down all containers.
- `psycopg` (v3) needs system libraries on slim images; always add `libpq-dev` and `gcc` to the Dockerfile.
- Click arguments are only useful if you don't override them with hardcoded values inside the function.
- Use `psycopg2-binary` for simplicity in learning projects; switch to `psycopg` v3 for production/async workloads.

---

## Project 2 — HeyEtsy & Everbee Scraper

Collects Etsy product listing data — images, tags, reviews, and sales analytics — from HeyEtsy and Everbee, then exports results to CSV/Excel and syncs to Trello.

### What It Does

| Script | Description |
|---|---|
| `heyetsy_image_scraper.py` | Scrapes listing image URLs from HeyEtsy using Selenium |
| `heyetsy_image_v2_scraper.py` | Updated image scraper with improved pagination and retry logic |
| `heyetsy_bulk_downloader.py` | Bulk-downloads listing images from collected URLs |
| `heyetsy_tags_scraper.py` | Extracts product tags from HeyEtsy listing pages |
| `everbee_data_scraper.py` | Scrapes product analytics (price, reviews, sales, favourites) from Everbee's virtual-scroll grid |
| `everbee_api_scraper.py` | Pulls Everbee data via its internal API |
| `everbee_api_shop_scraper.py` | Scrapes shop-level analytics from Everbee |
| `etsy_review_scraper.py` | Collects buyer reviews from Etsy listing pages |
| `etsy_check_hidden_listing.py` | Detects and flags hidden/removed Etsy listings |
| `trello_uploader*.py` | Uploads listing data and images to Trello cards |
| `csv_image_export.py` | Exports image URLs to CSV |
| `image_excel_export.py` | Builds Excel reports with embedded images |

### Data Collected (Everbee)

| Column | Description |
|---|---|
| `shop_name` | Etsy shop name |
| `price` | Listing price |
| `total_reviews` | Total number of reviews |
| `listing_age` | How old the listing is |
| `total_favorites` | Number of times favourited |
| `avg_reviews` | Average review score |
| `total_views` | Total listing views |
| `shop_age` | Shop age |
| `total_shop_sales` | Total sales for the shop |
| `category` | Product category |
| `listing_type` | Physical / digital |

### Technical Notes

- Everbee uses a **MUI DataGrid virtual scroller** — standard pagination doesn't work. The scraper scrolls incrementally and deduplicates rows by tracking seen IDs.
- Selenium is used with **Microsoft Edge** in headless mode; both HeyEtsy and Everbee require a logged-in browser session.
- Checkpointing flushes to disk every N rows (`CHECKPOINT_EVERY`) to survive interruptions — resume picks up from the last saved row.
- Trello upload uses the Trello REST API to create cards and attach image files per listing.

### Tech Stack

- **Scraping:** Selenium (Edge), BeautifulSoup4, Requests
- **Data processing:** Pandas, openpyxl
- **Integration:** Trello REST API
- **Package manager:** uv

---

## Project 3 — Mockup Generator

Extracts product artwork from Etsy shirt mockup images: crops the art region, removes the background, and saves transparent PNGs ready for re-use.

### Pipeline

```
heyetsy_image_urls.csv
        │
        ▼
annotate_crop.py          ← user marks crop regions interactively (OpenCV window)
        │ crop_coords.csv
        ▼
extract_art.py            ← downloads images, crops, removes background (rembg)
        │
        ▼
extracted_art/{id}_art.png   ← transparent PNG output
extract_log.csv              ← per-listing result log
```

### Scripts

| Script | Description |
|---|---|
| `annotate_crop.py` | Opens each listing image in an OpenCV window; user draws a bounding box over the art region; coordinates are saved to `crop_coords.csv` |
| `extract_art.py` | Reads `crop_coords.csv`, downloads the source image, crops the art region, runs `rembg` to remove the background, saves a transparent PNG |
| `remove_background.py` | Standalone background removal utility using `rembg` — can process a single file or a batch directory |

### Output

- `extracted_art/{listing_id}_art.png` — transparent PNG of the extracted artwork
- `extracted_art/previews/` — pre-removal crop previews for inspection
- `crop_coords.csv` — annotation log `(listing_id, x, y, w, h)`
- `extract_log.csv` — per-listing processing result

### Tech Stack

- **Image processing:** OpenCV, Pillow
- **Background removal:** rembg (ONNX Runtime, `u2net` model)
- **Data I/O:** Pandas, Requests
- **Package manager:** uv

---

## Repo-Level Structure

```
topcv-data-pipeline/
├── job-scraper/              # TopCV job scraper
├── data-ingestion/           # PostgreSQL ingestion
├── data-files/               # Raw job data (CSV/XLSX)
├── product-scraper/          # HeyEtsy & Everbee scrapers
├── mockup-generator/         # Art extraction pipeline
├── images/                   # Downloaded listing images
├── extracted_art/            # Processed transparent PNGs
├── docker-compose.yaml
├── Dockerfile
├── pyproject.toml            # uv-managed dependencies
└── .python-version
```

## Shared Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.12+ |
| Package manager | uv |
| Scraping | Selenium, BeautifulSoup4, Requests |
| Data processing | Pandas, openpyxl |
| Image processing | OpenCV, Pillow, rembg |
| Database | PostgreSQL 18 (Docker) |
| Containerisation | Docker / Docker Compose |
| Integration | Trello REST API |

---

## Future Roadmap

### TopCV — Job Roles Expansion
- Expand to Backend, Frontend, Fullstack, DevOps roles
- Extend beyond TopCV to ITviec, VietnamWorks, LinkedIn
- Unified `fact_job_postings` table with `role_category` column

### TopCV — Analysis & Reporting
- Salary normalisation into min/max numeric values
- Skill demand tracking by parsing `desc_yeucau`
- Interactive dashboard via Metabase or Apache Superset

### TopCV — Orchestration & Data Warehouse
- Apache Airflow DAG for daily incremental scraping
- Star schema with `dim_company`, `dim_location`, `dim_skills`, `fact_job_postings`
- dbt transformation layer + BigQuery/Snowflake migration

### HeyEtsy & Everbee — Enhancements
- Auto-login and session refresh for long scraping runs
- Scheduled daily sync to keep listing data fresh
- Keyword and trend analysis across collected tags

### Mockup Generator — Enhancements
- Batch annotation mode without manual bounding box input
- AI-assisted crop detection to auto-locate art regions
- Replicate API integration for higher-quality background removal
