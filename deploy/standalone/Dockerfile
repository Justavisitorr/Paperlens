FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    PAPERLENS_SHARED_CACHE=/data/shared-cache PLAYWRIGHT_BROWSERS_PATH=/opt/browsers TMPDIR=/data/tmp
WORKDIR /app
COPY LICENSE pyproject.toml README.md requirements.txt /app/
COPY src /app/src
# Build the reviewed frontend before packaging: cd frontend && npm ci && npm run build.
COPY frontend/dist/index.html /app/src/paperlens/assets/dashboard.html
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home paperlens \
    && mkdir -p /data \
    && chown -R paperlens:paperlens /data
USER paperlens
CMD ["python", "-m", "paperlens.standalone", "--data", "/data", "--host", "0.0.0.0", "--port", "8080"]
