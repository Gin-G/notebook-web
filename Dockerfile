FROM python:3.11-slim AS base

# System dependencies (git for repo sync)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Non-root user
RUN useradd -m -u 1000 appuser \
    && mkdir -p /tmp/notebook-cache \
    && chown -R appuser:appuser /app /tmp/notebook-cache

USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
