# Portable image for the deterministic core (search, watches, charts,
# dashboard). OpenClaw/Maritime run the conversational layer separately.
# Build:  docker build -f deploy/Dockerfile -t travel-agent .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/travel_agent.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY bin ./bin
COPY skills ./skills

VOLUME ["/data"]

# Default: process due reminders and price watches (what a scheduler runs).
# Override for other commands, e.g.:
#   docker run travel-agent python -m src.cli dashboard --port 8090
CMD ["python", "-m", "src.cli", "monitor-run"]
