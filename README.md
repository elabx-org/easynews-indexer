# [☕ Please support my work on Buy Me a Coffee](https://buymeacoffee.com/gaikwadsank)

# Easynews Newznab-like server

> **elabx-org fork.** Maintained independently of upstream. Built and deployed by Komodo as
> `ghcr.io/elabx-org/easynews-indexer`. Changes vs upstream: sample items carry explicit
> categories (so Sonarr's add-time test passes through Prowlarr), context-aware anime
> detection, a working `STRICT_MATCHING` env var, a keyless `GET /health`, a short-TTL
> search-result cache (`CACHE_TTL_SECONDS`), and configurable defaults (`EASYNEWS_BASE_URL`,
> `DEFAULT_MIN_SIZE_MB`, `DEFAULT_LIMIT`, `GUNICORN_WORKERS`), the Easynews 3.0 search API
> with an account-wide concurrency cap, sample-clip filtering, and an optional anime-only mode.

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
Invalid values are ignored with a warning and the default is used.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EASYNEWS_USER` | (required) | Easynews username |
| `EASYNEWS_PASS` | (required) | Easynews password |
| `NEWZNAB_APIKEY` | `testkey` | API key Prowlarr/Newznab clients must send |
| `PORT` | `8081` | Listen port |
| `EASYNEWS_BASE_URL` | `https://members.easynews.com` | Easynews host used for login, search and NZB download; must start with `http://` or `https://`; trailing slash stripped |
| `DEFAULT_MIN_SIZE_MB` | `100` | Minimum file size in MB: the default when `?minsize=` is absent, and the floor for any `?minsize=` value |
| `MAX_RESULTS` | `250` | Results fetched from Easynews per search and the ceiling for `DEFAULT_LIMIT` (bounded 100-1000). On the 3.0 API this fetches `ceil(MAX_RESULTS/100)` pages, so 500 costs 5 page requests per search. Raise for NZBgeek-level volume. |
| `DEFAULT_LIMIT` | `100` | Results returned when `?limit=` is absent and the hard maximum for `?limit=`; advertised as `max`/`default` in caps `<limits>`. Capped at `MAX_RESULTS`. |
| `STRICT_MATCHING` | `1` | Strict title matching for `t=movie` / `t=tvsearch` (`0` to disable) |
| `EASYNEWS_API_VERSION` | `3.0` | Search API. `3.0` (fixed 100 items/page, richer fields, ~10 concurrent searches) ranks by relevance as long as no sort parameter is sent (sending `s1=relevance` silently switches it to filename order). `2.0` is the legacy Solr endpoint. |
| `EASYNEWS_MAX_CONCURRENT_SEARCHES` | `2` | In-flight search cap per process. Easynews allows 2 on 2.0 (about 10 on 3.0) per account and returns empty bodies over the cap. |
| `GUNICORN_THREADS` | `8` | Threads per worker (Docker image). Keep `GUNICORN_WORKERS=1` so the search cap applies account-wide. |
| `GUNICORN_WORKERS` | `1` | Gunicorn worker processes (Docker image only); a non-positive or non-integer value falls back to 1. Keep at 1 so the Easynews search cap applies account-wide. |

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

## Configuration

All settings are environment variables (a `.env` file in the working directory is also read).

| Variable | Default | Purpose |
|----------|---------|---------|
| `EASYNEWS_USER` | (required) | Easynews username |
| `EASYNEWS_PASS` | (required) | Easynews password |
| `NEWZNAB_APIKEY` | `testkey` | API key Prowlarr must send (`apikey=` or `X-Api-Key`) |
| `PORT` | `8081` | Listen port |
| `STRICT_MATCHING` | `1` | Default title strictness for `t=movie` / `t=tvsearch` (per-request `strict=0|1` overrides) |
| `ANIME_ONLY` | `0` | Advertise only the anime category (5070) in caps and return only anime results, so Prowlarr maps the indexer to anime categories and FusionHA/Sonarr query it for anime only, with no application tags. Includes fansub releases that use `SxxEyy` numbering. |
| `CACHE_TTL_SECONDS` | `120` | Seconds to cache raw Easynews search responses; `0` disables caching |

### Search cache

Sonarr/Radarr (especially several instances behind one Prowlarr) tend to fire the
same search repeatedly within minutes. Identical searches — same query, page,
sort and page size as sent to Easynews — are served from an in-memory cache for
`CACHE_TTL_SECONDS` and don't hit Easynews again. Notes:

- Only real Easynews searches are cached. The empty-query / `q=test` sample
  fallback and `t=get` NZB downloads are never cached.
- Errors from Easynews are never cached; the next request retries.
- Expired entries are refreshed, not served. The cache is bounded (256 entries,
  oldest evicted).
- The cache is **per process**. The Docker image runs gunicorn with 4 sync
  workers, so each worker keeps its own cache and a repeated search may reach
  Easynews up to 4 times before every worker is warm.

## Sample clips

Results whose name carries a `sample` token as a prefix or in the second half (`...-sample`, `...edith.sample`, `sample-<release>`) are dropped, as are files shorter than 60 seconds. 4K sample clips exceed the size floor, so without this Sonarr could grab a one-minute clip as an episode.

## Endpoints

- Health (no API key): `GET /health` → `{"status":"ok"}`
- Caps: `GET /api?t=caps&apikey=<key>` (`<limits max/default>` reflects `DEFAULT_LIMIT`)
- Search (video-only): `GET /api?t=search&q=<query>&apikey=<key>&limit=<n>&offset=<n>&minsize=<MB>`
	- Default `limit=DEFAULT_LIMIT`, `minsize=DEFAULT_MIN_SIZE_MB` (MB); `minsize` below `DEFAULT_MIN_SIZE_MB` is raised to it, `limit` above `DEFAULT_LIMIT` is lowered to it
	- Blank or non-integer `limit`, `offset` and `minsize` values fall back to their defaults (`offset` defaults to 0)
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
