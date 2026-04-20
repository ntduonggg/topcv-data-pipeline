# FROM python:3.13.10-slim AS builder

# WORKDIR /app

# COPY requirements.txt .
# RUN pip install --user --no-cache-dir -r requirements.txt

# FROM python:3.13.10-slim

# WORKDIR /app

# # Copy uv binary from the official uv image
# COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# # Use local user installs from builder
# COPY --from=builder /root/.local /root/.local
# ENV PATH="/root/.local/bin:$PATH"

# COPY job-scraper/ ./job-scraper/
# COPY data-ingestion/ ./data-ingestion/

# CMD ["sh", "-c", "uv run python job-scraper/topcv_data_scraper.py && uv run python data-ingestion/ingest_data.py"]

FROM python:3.13.11-slim


RUN apt-get update && apt-get install -y libpq-dev gcc
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

WORKDIR /web-scrapers
ENV PATH="/web-scrapers/.venv/bin:$PATH"

COPY pyproject.toml .python-version uv.lock ./
RUN uv sync --locked

COPY job-scraper/ ./job-scraper/
COPY data-ingestion/ ./data-ingestion/
COPY data-files/ ./data-files/ 

ENTRYPOINT ["uv", "run", "python", "data-ingestion/ingest_data.py"]