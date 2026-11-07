FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Tesseract OCR (Apache-2.0) for scanned filings, English data only. Installed from Debian's
# repositories; the extractor calls the `tesseract` command (see app/docintel/ocr.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Run as an unprivileged user with a writable data directory (cache, documents).
RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
ENV DATA_DIR=/app/data
USER appuser

EXPOSE 8000
# Apply pending migrations, then start the API.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
