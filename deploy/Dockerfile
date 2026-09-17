# Portable image: runs on Maritime today, DigitalOcean (or any Docker host)
# later. The deterministic core is plain Python; OpenClaw mounts/copies the
# skill from skills/travel-agent.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/travel_agent.sqlite3

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir "pydantic>=2.5" "httpx>=0.27"

COPY src ./src
COPY skills ./skills

# Persist SQLite outside the container filesystem.
VOLUME ["/data"]

# Default: process due monitoring tasks (what a Maritime scheduled trigger
# or cron invokes). Override the command for other tools, e.g.:
#   docker run travel-agent python -m src.cli status --trip trip_abc
CMD ["python", "-m", "src.cli", "monitor-run"]
