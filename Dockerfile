# Drinks Tracker — single self-hosted image (collection, discovery, API).
FROM python:3.11-slim

# Minimal cron runner for the scheduled services.
ARG SUPERCRONIC_VERSION=v0.2.33
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY pyproject.toml requirements-api.txt ./
COPY beverage_feed ./beverage_feed
COPY crontabs ./crontabs
COPY data ./data

# Editable install of the local package plus the read-only API dependencies.
RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir -r requirements-api.txt

ENV DRINKS_DATABASE=/data/feed.sqlite \
    PYTHONUNBUFFERED=1

VOLUME /data

# supercronic crashes with "Failed to fork exec" when it runs as the
# container's PID 1; keeping sh as PID 1 (no exec) sidesteps it. Signals:
# docker stop falls back to SIGKILL after the grace period, which is fine
# for scheduled collection.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["supercronic /app/crontabs/collector.cron"]
