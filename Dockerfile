FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC

# Runtime deps are stdlib-only (see pyproject.toml); no pip install needed.
COPY . .

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p data/cache reports/daily

# F7 (hardening): a dedicated non-root user for the app. The container STARTS as root only so
# the entrypoint can repair ownership of a pre-existing persistent volume (which the image cannot
# seed), then it drops to appuser before running the scheduler. See docker/entrypoint.sh. The
# application process itself always runs as UID 10001 — verified by the entrypoint's UID guard.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
# NOTE: deliberately NO `USER appuser` here. Dropping privileges in the entrypoint (rather than
# the image) is what lets an already-existing root-owned volume be repaired at startup.
#
# Consequence for operators: `docker exec <container> <cmd>` now defaults to root (it uses the
# image USER, not PID 1). PID 1 and the Python process still run as UID 10001. For routine
# inspection, run the shell unprivileged explicitly:
#     docker exec --user 10001 <container> <command>
# Starting the container directly as UID 10001 (e.g. a `user:` override) is refused by the
# entrypoint, because that would skip the ownership repair; see docker/entrypoint.sh.

# Reports and the price cache live under these paths; mount Coolify persistent
# volumes on /app/reports and /app/data to survive rebuilds.
ENTRYPOINT ["./docker/entrypoint.sh"]
