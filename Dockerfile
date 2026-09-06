# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8081

# GUNICORN_WORKERS must be a positive integer; anything else falls back to 4.
CMD ["sh", "-c", "W=${GUNICORN_WORKERS:-4}; case \"$W\" in ''|0|*[!0-9]*) echo \"Ignoring GUNICORN_WORKERS='$W': not a positive integer, using 4\" >&2; W=4;; esac; exec gunicorn --bind 0.0.0.0:${PORT} --workers $W server:APP"]
