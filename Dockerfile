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
COPY docker-entrypoint.sh ./

# Editable install of the local package plus the read-only API dependencies.
RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir -r requirements-api.txt \
    && chmod +x /app/docker-entrypoint.sh

ENV DRINKS_DATABASE=/data/feed.sqlite \
    PYTHONUNBUFFERED=1

VOLUME /data

# See docker-entrypoint.sh for the PID 1 / supercronic and one-off command
# handling.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["supercronic", "/app/crontabs/collector.cron"]
