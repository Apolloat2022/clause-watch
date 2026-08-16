# Single image for all three entrypoints (API, ingest worker, scanner); the
# Container App and the two Jobs differ only in the command they run. One image
# means one build and one thing to keep patched.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir -e ".[api,azure,llm]"

# Non-root: Container Apps does not require it, but there is no reason to run
# as root and every reason not to.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Overridden by the Jobs, which run:
#   python -m app.jobs.ingest_worker
#   python -m app.jobs.obligation_scanner
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
