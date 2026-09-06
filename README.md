# [☕ Please support my work on Buy Me a Coffee](https://buymeacoffee.com/gaikwadsank)

# Easynews Newznab-like server

> **elabx-org fork.** Maintained independently of upstream. Built and deployed by Komodo as
> `ghcr.io/elabx-org/easynews-indexer`. Changes vs upstream: sample items carry explicit
> categories (so Sonarr's add-time test passes through Prowlarr), context-aware anime
> detection, a working `STRICT_MATCHING` env var, a keyless `GET /health`, and
> configurable defaults (`EASYNEWS_BASE_URL`, `DEFAULT_MIN_SIZE_MB`, `DEFAULT_LIMIT`,
> `GUNICORN_WORKERS`).

Flask server that bridges Easynews search to a Newznab-like API so you can add it to Prowlarr as a custom indexer and download NZBs. Video-only, sorts by relevance, returns as many results as possible, and filters files smaller than `DEFAULT_MIN_SIZE_MB` (100 MB by default).

## Setup (Local)

1. Create and activate a Python 3.11+ virtual environment:

```
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Linux / macOS (bash/zsh)
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```
pip install -r requirements.txt
```

3. Configure credentials and API key. Create a `.env` file in the repo root:

```
EASYNEWS_USER=your_easynews_username
EASYNEWS_PASS=your_easynews_password
NEWZNAB_APIKEY=testkey
```

4. Run the server:

```
python server.py
```

It starts on `http://127.0.0.1:8081`.

## Configuration

All settings are environment variables (a `.env` file in the working directory is also read; see `.env.example`).
Invalid integer values are ignored with a warning and the default is used.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EASYNEWS_USER` | (required) | Easynews username |
| `EASYNEWS_PASS` | (required) | Easynews password |
| `NEWZNAB_APIKEY` | `testkey` | API key Prowlarr/Newznab clients must send |
| `PORT` | `8081` | Listen port |
| `EASYNEWS_BASE_URL` | `https://members.easynews.com` | Easynews host used for login, search and NZB download; trailing slash stripped |
| `DEFAULT_MIN_SIZE_MB` | `100` | Minimum file size in MB: the default when `?minsize=` is absent, and the floor for any `?minsize=` value |
| `DEFAULT_LIMIT` | `100` | Results returned when `?limit=` is absent; also advertised as `max`/`default` in caps `<limits>` |
| `STRICT_MATCHING` | `1` | Strict title matching for `t=movie` / `t=tvsearch` (`0` to disable) |
| `GUNICORN_WORKERS` | `4` | Gunicorn worker processes (Docker image only) |

## Setup (Docker)


### Pull from GitHub Container Registry

```
docker pull ghcr.io/sanket9225/easynews_as_indexer:latest
```

Run the published image (Linux/macOS shells):

```
docker run --rm -d -p 8081:8081 \
	-e EASYNEWS_USER=your_easynews_username \
	-e EASYNEWS_PASS=your_easynews_password \
	-e NEWZNAB_APIKEY=testkey \
	-e PORT=8081 \
	-e STRICT_MATCHING=1 \
	ghcr.io/sanket9225/easynews_as_indexer:latest
```

> The published image currently includes `linux/amd64` and `linux/arm64` manifests.

Windows PowerShell equivalent:

```
docker run --rm -d -p 8081:8081 ^
	-e EASYNEWS_USER=your_easynews_username ^
	-e EASYNEWS_PASS=your_easynews_password ^
	-e NEWZNAB_APIKEY=testkey ^
	-e PORT=8081 ^
	-e STRICT_MATCHING=1 ^
	ghcr.io/sanket9225/easynews_as_indexer:latest
```

To tail logs from the detached container run `docker logs -f <container-id>`.

## Endpoints

- Health (no API key): `GET /health` → `{"status":"ok"}`
- Caps: `GET /api?t=caps&apikey=<key>` (`<limits max/default>` reflects `DEFAULT_LIMIT`)
- Search (video-only): `GET /api?t=search&q=<query>&apikey=<key>&limit=<n>&minsize=<MB>`
	- Default `limit=DEFAULT_LIMIT`, `minsize=DEFAULT_MIN_SIZE_MB` (MB); `minsize` below `DEFAULT_MIN_SIZE_MB` is raised to it
	- Also supports `t=movie` and `t=tvsearch`
	- **Strict matching** is enabled by default for `t=movie` and `t=tvsearch` (requires title to contain all query words); disabled for plain `t=search`
	- Optional `strict=0|1` overrides title matching strictness per request
	- Movie search accepts `year=<YYYY>` to bias results; TV search accepts `season=<NN>` and `ep=<NN>` (automatically appended as `SxxEyy` in the Easynews query)
- Download NZB: `GET /api?t=get&id=<encoded>&apikey=<key>`
	- Filename equals the item title

## Prowlarr integration

Add a Newznab (generic) indexer in Prowlarr:
- URL: `http://127.0.0.1:8081`
- API Key: the same key in your `.env` (e.g., `testkey`)

---

## [☕ If this project helps you, consider buying me a coffee](https://buymeacoffee.com/gaikwadsank)
