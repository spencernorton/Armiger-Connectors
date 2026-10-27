# Bitmagnet + Armiger Ultra Connector

The connector polls stock [Bitmagnet](https://github.com/bitmagnet-io/bitmagnet), batch-fetches file lists (`torrent.files`), evaluates all 9 Armiger neural heads and Ultra plugins, and groups results into GraphQL tag (`torrent.putTags`) and deletion (`torrent.delete`) mutations. It also exposes a pre-grab filtered Torznab proxy (`:8097/torznab`) and an HTTP classification bridge (`:8096`).

> **Looking for the prebuilt all-in-one container image?** See the [Armiger Bitmagnet setup guide](https://github.com/spencernorton/armiger/blob/main/examples/bitmagnet/README.md) and [configuration reference](https://github.com/spencernorton/armiger/blob/main/docs/bitmagnet-configuration.md), which use `ghcr.io/spencernorton/armiger:3.3.1` with the Ultra model pre-bundled in `/models`.

---

## 1. Setup

### Option A: Turnkey Docker Compose stack (PostgreSQL + Bitmagnet + Armiger)

From a clone of this repository (or after downloading [`bitmagnet/docker-compose.yml`](docker-compose.yml)):

```bash
export POSTGRES_PASSWORD="change_me"
docker compose -f bitmagnet/docker-compose.yml up -d
```

This starts PostgreSQL (`:5432` internal), Bitmagnet (`:3333` Web UI / GraphQL / raw Torznab and `:3334` DHT), the Armiger connector (`:8096`), and the filtered Torznab proxy (`:8097`). Model files are downloaded on first start and cached in the `armiger_data` volume.

### Option B: Connect to an existing Bitmagnet installation

Install [Armiger](https://github.com/spencernorton/armiger#choose-your-setup) (`pip install 'armiger[ultra]' && python -m armiger download`), then run the connector against your existing Bitmagnet GraphQL and Torznab endpoints:

```bash
export BITMAGNET_GRAPHQL_URL=http://localhost:3333/graphql
export UPSTREAM_TORZNAB_URL=http://localhost:3333/torznab
python3 bitmagnet/bitmagnet_connector.py
```

---

## 2. How to use it

1. **Filter by `armiger-*` tags in Bitmagnet (`http://localhost:3333`):**
   The poller labels untagged torrents with kebab-case tags (bounded to Bitmagnet's 30-character tag limit):
   - **Category:** `armiger-movie`, `armiger-tv-show`, `armiger-music`, `armiger-game`, `armiger-software`, `armiger-ebook`, `armiger-audiobook`, `armiger-comic`, `armiger-course`, `armiger-xxx`, `armiger-unknown`
   - **Audio language:** `armiger-lang-english`, `armiger-lang-non-english`, `armiger-lang-multi-or-unknown`
   - **Title script:** `armiger-title-english`, `armiger-title-non-english`
   - **Swarm viability:** `armiger-viability-alive-viable`, `armiger-viability-unknown-cold`, plus 30-char hash-suffixed tags for `degraded_viable`, `suspect_stalled`, and `dead_known_zero`
   - **Anime & subtitles:** `armiger-anime`, `armiger-dub-dual-audio-or-dub`, `armiger-dub-subbed-only`, `armiger-dub-raw-or-vernacular`, `armiger-eng-subs`
2. **Use the filtered Torznab proxy in Prowlarr, Sonarr, Radarr, Lidarr, or Readarr (`http://localhost:8097/torznab`):**
   Point your indexer client at `http://localhost:8097/torznab` (or `http://armiger-bitmagnet-connector:8097/torznab` on the same Docker network) instead of `:3333/torznab`. The proxy automatically blocks disguised executables (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`, `.bat`, `.ps1`, etc.), safety blocks, honeypot traps, and `junk` items, applies optional audio-language and dead-swarm rules, and annotates admitted items with `armiger_category`, `armiger_lang`, `armiger_title_lang`, `armiger_viability`, and `armiger_survival_prob`.
3. **Inspect health and classify on demand (`http://localhost:8096`):**
   - `GET http://localhost:8096/healthz` — returns service status, model variant, catalog/adapter readiness, `active_seq`, `active_bundle_merkle_root`, `model_source`, `last_dht_check_ts`, and `sharing_status`.
   - `POST http://localhost:8096/v1/bitmagnet/classify` — classify a batch (`{"torrents": [...], "applyMutations": false}`) and inspect the resulting predictions and GraphQL mutations.

---

## 3. Environment variables

### Stack, ports, and Bitmagnet settings (`bitmagnet/docker-compose.yml`)

| Variable | Compose default | What it does |
| --- | --- | --- |
| `POSTGRES_DB` | `bitmagnet` | PostgreSQL database name for Bitmagnet. |
| `POSTGRES_USER` | `postgres` | PostgreSQL user for Bitmagnet. |
| `POSTGRES_PASSWORD` | `bitmagnet` | PostgreSQL password shared by `postgres` and `bitmagnet`. |
| `BITMAGNET_HTTP_PORT` | `3333` | Host port mapped to Bitmagnet's Web UI, GraphQL API, and raw Torznab feed. |
| `BITMAGNET_DHT_PORT` | `3334` | Host TCP/UDP port mapped to Bitmagnet's DHT crawler. |
| `TMDB_API_KEY` | `""` (empty) | Optional TMDB API key for Bitmagnet's built-in TMDB lookup. |
| `DHT_CRAWLER_SCALING_FACTOR` | `10` | Bitmagnet DHT crawler concurrency multiplier. Scale `BITMAGNET_BATCH_SIZE` proportionally if raising this above `30`. |
| `ARMIGER_BRIDGE_PORT` | `8096` | Host port mapped to the connector's main HTTP listener (`8096`). |
| `ARMIGER_HOST_MODEL_DIR` | `.` | Optional host model directory mounted read-only at `/host_model`. |

### Polling and Bitmagnet connection (`bitmagnet_connector.py`)

| Variable | Default (`docker-compose.yml` / CLI) | What it does |
| --- | --- | --- |
| `ARMIGER_CONNECTOR_MODE` | `both` | `both` runs the background GraphQL poller and HTTP/Torznab listeners. `http` runs only HTTP/Torznab listeners. `poll` runs only the background poller. |
| `BITMAGNET_POLL_SECONDS` | `5` (`5.0`) | Seconds between polling cycles. |
| `BITMAGNET_BATCH_SIZE` | `100` | Maximum untagged Bitmagnet torrents classified per cycle. Pages via `offset`/`hasNextPage` across multi-batch 60-second Bitmagnet `persistTorrents` flushes. |
| `BITMAGNET_GRAPHQL_URL` | `http://bitmagnet:3333/graphql` (Compose) / `http://localhost:3333/graphql` (CLI) | Bitmagnet GraphQL endpoint for search, file-list queries, tag writes, and deletions. |
| `UPSTREAM_TORZNAB_URL` | `http://bitmagnet:3333/torznab` (Compose) / `http://localhost:3333/torznab` (CLI) | Upstream Bitmagnet Torznab endpoint proxied and filtered by Armiger. |

Stock Bitmagnet flushes newly crawled DHT torrents in batches of up to 1,000 items or every 60 seconds. With `BITMAGNET_POLL_SECONDS=5` and `BITMAGNET_BATCH_SIZE=100`, the poller pages past items already tagged earlier in the active flush across consecutive cycles (up to 1,200 torrents/minute). If you raise `DHT_CRAWLER_SCALING_FACTOR` above `30` or poll less frequently, increase `BITMAGNET_BATCH_SIZE` so `(60 / BITMAGNET_POLL_SECONDS) * BITMAGNET_BATCH_SIZE` exceeds your crawler's per-minute intake.

### Torznab filtering and index pruning (`bitmagnet_connector.py`)

| Variable | Default (`docker-compose.yml` / CLI) | What it does |
| --- | --- | --- |
| `ARMIGER_REQUIRE_ENGLISH_AUDIO` | `false` | When `true`, blocks Torznab search results classified as `non_english` audio (subject to `ARMIGER_ALLOW_ENGLISH_SUBS`). |
| `ARMIGER_ALLOW_ENGLISH_SUBS` | `true` | When `ARMIGER_REQUIRE_ENGLISH_AUDIO=true`, permits `non_english` audio items if English subtitles (`has_eng_subs`) are detected. |
| `ARMIGER_REJECT_DEAD_SWARMS` | `true` (`false` in `spencernorton/armiger` Compose) | When `true`, excludes results classified as `dead` from Torznab responses without deleting them from Bitmagnet. |
| `ARMIGER_PRUNE_MALWARE` | `true` (`false` in `spencernorton/armiger` Compose) | When `true`, deletes torrents flagged as unsafe (`csam_safe=False`), disguised executables, `junk`, or honeypot traps from Bitmagnet's index via `mutation TorrentDelete`. |
| `ARMIGER_PRUNE_DEAD_SWARMS` | `false` | When `true`, deletes torrents classified as `dead` from Bitmagnet's index via `mutation TorrentDelete`. |

### Model, catalog, listeners, and distributed updates

| Variable | Default (`docker-compose.yml` / CLI) | What it does |
| --- | --- | --- |
| `ARMIGER_MODEL_DIR` | `/data/armiger/model` (Compose) / `.` (CLI) | Directory containing the model bundle. |
| `ARMIGER_VARIANT` | `ultra` | `ultra` (FP32 ONNX + Ultra plugins) or `int8` / `cpu` / `armiger` (INT8 ONNX + Ultra plugins). |
| `ARMIGER_AUTO_DOWNLOAD` | `true` | Automatically downloads and verifies missing model files on startup. |
| `ARMIGER_USE_ZERO_CUE_CATALOG` | `true` | Preloads `validated_zero_cue_catalog.json` at startup (`false` defers until first lookup). |
| `ARMIGER_HF_REPO` | `NorviTech/armiger-ultra` | Hugging Face repository ID used when bootstrapping via the standalone legacy loader. |
| `ARMIGER_CONNECTOR_HOST` | `0.0.0.0` | Bind address for HTTP and Torznab listeners (fallback alias: `ARMIGER_BRIDGE_HOST`). |
| `ARMIGER_CONNECTOR_PORT` | `8096` | Main HTTP listener port for `/healthz`, `/v1/bitmagnet/classify`, and `/torznab` (fallback alias: `ARMIGER_BRIDGE_PORT`). |
| `ARMIGER_PROXY_PORT` | `8097` | Dedicated Torznab proxy port (`/torznab` and `/api`). |
| `ARMIGER_ENABLE_TORZNAB_PROXY` | `true` | Starts the dedicated `ARMIGER_PROXY_PORT` listener in `both` or `http` mode. |
| `ARMIGER_DHT_AUTO_UPDATE` | `true` | Enables background BEP 44/46 polling for newer signed model/catalog releases, 500-item canary verification, and atomic hot-swap into `ARMIGER_RUNTIME_CACHE_DIR`. |
| `ARMIGER_DHT_POLL_INTERVAL_SEC` | `14400` | Seconds between background BEP 44/46 DHT release checks (default 4 hours). |
| `ARMIGER_RUNTIME_CACHE_DIR` | `/tmp/armiger-live-models-<uid>` | Private (`0700`) writable directory where verified DHT live-update overlays are staged. |
| `ARMIGER_DHT_RSI_MODE` | `pull_only` | Distributed update policy: `pull_only`, `off`, or `anonymous_contribute`. |
| `ARMIGER_DHT_SHARE_OPT_IN` | `false` (`0`) | Strict opt-in switch (`true`/`1`/`yes` to enable; `false` by default). Blocks all outbound contribution packets when `false`. |
