FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC

# Runtime deps are stdlib-only (see pyproject.toml); no pip install needed.
COPY . .

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p data/cache reports/daily

# Reports and the price cache live under these paths; mount Coolify persistent
# volumes on /app/reports and /app/data to survive rebuilds.
ENTRYPOINT ["./docker/entrypoint.sh"]
