# TopCV Job Scraper & Data Pipeline

Scraping Data Engineer job listings from TopCV, cleaning the data, and ingesting it into PostgreSQL — fully containerised with Docker.

---

## 1) Business Context

The goal is to collect Data Engineer job postings from TopCV (Vietnam's largest job platform), store them in a structured PostgreSQL database, and make them available for analysis. The pipeline handles scraping, data cleaning, encoding issues with Vietnamese text, and automated ingestion — all running inside Docker containers connected via a shared network.

---

## 2) What This Repo Contains

```
web-scrapers/
├── job-scraper/
│   ├── topcv_data_scraper.py       # scrapes job listings from TopCV
│   ├── topcv_scraper.py            # scraper utilities
│   └── topcv_job_urls.csv          # collected job URLs
├── data-ingestion/
│   └── ingest_data.py              # cleans and ingests CSV into PostgreSQL
├── data-files/
│   ├── topcv_data_jobs.csv         # raw scraped data
│   └── topcv_data_jobs.xlsx        # Excel version
├── Dockerfile                      # builds the pipeline image
├── pyproject.toml                  # dependencies managed by uv
├── uv.lock
└── .python-version
```

---

## 3) Data Collected

Each job listing captures the following fields:

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

---

## 4) Technical Challenges & Solutions

- **Vietnamese text encoding** — `charmap` codec errors when reading/writing Vietnamese characters. Solution: always use `encoding="utf-8"` for file I/O and `encoding="utf-8-sig"` for CSV exports.
- **Mixed date formats** — deadline field stored as string (`DD/MM/YYYY`). Solution: cast to `DATE` in PostgreSQL after ingestion using `TO_DATE(deadline, 'DD/MM/YYYY')`.
- **Docker networking** — pipeline container couldn't reach PostgreSQL using `localhost`. Solution: both containers run on the same Docker network (`pg-network`), and the host is referenced by container name (`pgdatabase`).
- **Port conflict** — local PostgreSQL installation occupied port 5432, blocking Docker. Solution: identified and killed the conflicting PID via `netstat` + `taskkill`.
- **psycopg vs psycopg2** — `psycopg` (v3) requires `libpq-dev` and `gcc` on slim images. Solution: added system dependencies in Dockerfile and used `postgresql+psycopg://` connection string.
- **Hardcoded credentials overriding CLI args** — click arguments were being ignored due to hardcoded values in the function body. Solution: removed hardcoded variables and relied solely on click-passed arguments.

---

## 5) Tech Stack

- **Language:** Python 3.13
- **Scraping:** BeautifulSoup4, Requests
- **Data processing:** Pandas
- **Database:** PostgreSQL 18 (Docker)
- **ORM/Ingestion:** SQLAlchemy + psycopg (v3)
- **CLI:** Click
- **Package manager:** uv
- **Containerisation:** Docker

---

## 6) How to Run / Reproduce

### Prerequisites
- Docker Desktop installed and running
- uv installed (`pip install uv`)

### Step 1 — Create the Docker network
```bash
docker network create pg-network
```

### Step 2 — Start PostgreSQL container
```bash
docker run -it --rm \
  --network=pg-network \
  --name=pgdatabase \
  -e POSTGRES_USER="root" \
  -e POSTGRES_PASSWORD="root" \
  -e POSTGRES_DB="topcv_data" \
  -p 5432:5432 \
  postgres:18
```

### Step 3 — Build the pipeline image
```bash
docker build -t topcv_pipeline:v001 .
```

### Step 4 — Run the pipeline
```bash
docker run -it \
  --network=pg-network \
  topcv_pipeline:v001 \
  --pg-user=root \
  --pg-pass=root \
  --pg-host=pgdatabase \
  --pg-port=5432 \
  --pg-db=topcv_data \
  --target-table=data_engineer_job
```

### Step 5 — Verify in pgcli
```bash
uv run pgcli postgresql://root:root@localhost:5432/topcv_data
```
```sql
SELECT * FROM data_engineer_job LIMIT 5;
```

---

## 7) Future Features & Roadmap

### Job Roles Expansion
- **Backend Engineer jobs** — expand scraping to Backend Engineer roles to compare required skills (Java, Node.js, Go) against Data Engineer demand
- **Frontend Engineer jobs** — collect Frontend postings to track UI/UX technology trends (React, Vue, Angular) across the Vietnamese market
- **Fullstack & Mobile roles** — include Fullstack, iOS, and Android listings for a complete picture of the tech hiring landscape
- **DevOps & Cloud roles** — scrape DevOps, SRE, and Cloud Engineer jobs to benchmark infrastructure skill demand
- **Multi-platform scraping** — extend beyond TopCV to ITviec, VietnamWorks, and LinkedIn for broader market coverage
- **Unified jobs table** — store all roles in a single `fact_job_postings` table with a `role_category` column for cross-role comparison and analysis

### Analysis & Reporting
- **Salary analysis** — extract and normalise salary ranges into min/max numeric values for comparison across roles, locations, and companies
- **Skill demand tracking** — parse `desc_yeucau` to extract and rank the most in-demand tools and technologies (SQL, Python, Spark, Airflow, etc.)
- **Dashboard** — build an interactive dashboard with Metabase or Apache Superset connected to PostgreSQL for real-time job market insights
- **Trend analysis** — track job posting volume over time to identify hiring trends by company, location, and experience level

### Orchestration
- **Apache Airflow** — schedule the scraper and ingestion pipeline as a DAG to run daily, with retries, alerting, and task dependencies
- **Dagster or Prefect** — alternative modern orchestrators with built-in observability and data lineage tracking
- **Incremental loading** — instead of full reload, only scrape and ingest new or updated job postings since the last run

### Data Warehouse
- **Dimensional modelling** — restructure the flat table into a star schema with `dim_company`, `dim_location`, `dim_skills`, and `fact_job_postings`
- **dbt (data build tool)** — add a transformation layer on top of PostgreSQL to clean, model, and document data with version-controlled SQL
- **BigQuery / Snowflake** — migrate from local PostgreSQL to a cloud data warehouse for scalability and analytics performance
- **Data lake** — store raw scraped HTML/JSON in AWS S3 or Google Cloud Storage before processing, enabling full reprocessing without re-scraping

### Data Quality
- **Great Expectations** — add automated data quality checks (null rates, value ranges, schema validation) at each pipeline stage
- **dbt tests** — add schema and custom tests directly in the dbt transformation layer

---

## 8) Key Learnings



- Always use `utf-8` encoding when working with Vietnamese text — `charmap` errors are silent killers on Windows.
- Docker containers communicate via container names on shared networks, not `localhost`.
- Check PIDs carefully before killing processes — killing Docker Desktop itself will bring down all containers.
- `psycopg` (v3) needs system libraries on slim images; always add `libpq-dev` and `gcc` to the Dockerfile.
- Click arguments are only useful if you don't override them with hardcoded values inside the function.
- Use `psycopg2-binary` for simplicity in learning projects; switch to `psycopg` v3 for production/async workloads.
