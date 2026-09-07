FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8081

# One worker process with threads: the Easynews concurrency cap
# (EASYNEWS_MAX_CONCURRENT_SEARCHES) is enforced per process, so extra worker
# processes would multiply it. GUNICORN_WORKERS / GUNICORN_THREADS must be
# positive integers; anything else falls back to 1 / 8.
CMD ["sh", "-c", "W=${GUNICORN_WORKERS:-1}; case \"$W\" in ''|0|*[!0-9]*) echo \"Ignoring GUNICORN_WORKERS='$W': not a positive integer, using 1\" >&2; W=1;; esac; T=${GUNICORN_THREADS:-8}; case \"$T\" in ''|0|*[!0-9]*) echo \"Ignoring GUNICORN_THREADS='$T': not a positive integer, using 8\" >&2; T=8;; esac; exec gunicorn --bind 0.0.0.0:${PORT} --workers $W --threads $T server:APP"]
