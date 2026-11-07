FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
