FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    PAPERLENS_SHARED_CACHE=/data/shared-cache PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
WORKDIR /app
COPY LICENSE pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home paperlens \
    && mkdir -p /data \
    && chown -R paperlens:paperlens /data
USER paperlens
CMD ["python", "-m", "paperlens.web_agent", "run", "--data", "/data"]
