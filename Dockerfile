FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC

# Runtime deps are stdlib-only (see pyproject.toml); no pip install needed.
COPY . .

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p data/cache reports/daily

# F7 (hardening): run as a non-root user. Named docker volumes seeded from these directories
# inherit this ownership, so /app/data and /app/reports stay writable by appuser.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Reports and the price cache live under these paths; mount Coolify persistent
# volumes on /app/reports and /app/data to survive rebuilds.
ENTRYPOINT ["./docker/entrypoint.sh"]
