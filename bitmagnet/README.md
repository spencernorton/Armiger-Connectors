# Bitmagnet + Armiger Ultra Connector

The connector polls stock [Bitmagnet](https://github.com/bitmagnet-io/bitmagnet), batch-fetches file lists (`torrent.files`), evaluates all 9 Armiger neural heads and Ultra plugins, groups results into GraphQL tag (`torrent.putTags`) and deletion (`torrent.delete`) mutations, and writes `contentType` hints back to Bitmagnet's `/import` endpoint whenever Bitmagnet's built-in parser left a torrent as `Unknown`. It also exposes a pre-grab filtered Torznab proxy (`:8097/torznab`) and an HTTP classification bridge (`:8096`).

> **Looking for the prebuilt all-in-one container image?** See the [Armiger Bitmagnet setup guide](https://github.com/spencernorton/armiger/blob/main/examples/bitmagnet/README.md) and [configuration reference](https://github.com/spencernorton/armiger/blob/main/docs/bitmagnet-configuration.md), for the current verified image and copy-paste Compose commands, with the Ultra model pre-bundled in `/models`.

---

## 1. Setup

### Option A: Copy-pasteable Docker Compose stack (PostgreSQL + Bitmagnet + Armiger)

All settings in [`bitmagnet/docker-compose.yml`](docker-compose.yml) are **pre-populated with literal default values** (no `.env` file or `${VAR}` placeholders required). Copy [`bitmagnet/docker-compose.yml`](docker-compose.yml) into a directory (or run from a clone of this repository), edit any value directly in the file if needed, and start the stack:

```bash
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
   - **Swarm viability:** `armiger-viability-alive-viable`, `armiger-viability-degraded`, `armiger-viability-stalled`, `armiger-viability-dead`, `armiger-viability-unknown-cold` *(legacy hash-truncated tags `armiger-viability-dea-c24281f7`, `armiger-viability-deg-7c170f8b`, and `armiger-viability-sus-cdc9d2c6` are automatically migrated and removed on startup)*
   - **Anime & subtitles:** `armiger-anime`, `armiger-dub-dual-audio-or-dub`, `armiger-dub-subbed-only`, `armiger-dub-raw-or-vernacular`, `armiger-eng-subs`
2. **Use the filtered Torznab proxy in Prowlarr, Sonarr, Radarr, Lidarr, or Readarr (`http://localhost:8097/torznab`):**
   Point your indexer client at `http://localhost:8097/torznab` (or `http://armiger-bitmagnet-connector:8097/torznab` on the same Docker network) instead of `:3333/torznab`. The proxy automatically blocks disguised executables (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`, `.bat`, `.ps1`, etc.), safety blocks, honeypot traps, and `junk` items, applies optional audio-language and dead-swarm rules, promotes unclassified (`Unknown` / `8000` `Other`) Torznab items to their Armiger-predicted Torznab category (`2000` `Movie`, `5000` `TV show`, `3000` `Music`, `3030` `Audiobook`, `7000` `E-book`, `7030` `Comic`, `4000` `Software`, `4050` `Game`, `6000` `XXX`), rescues matching `Unknown` items during category-restricted `tvsearch` / `movie` / `cat=` queries, and annotates admitted items with `armiger_category`, `armiger_lang`, `armiger_title_lang`, `armiger_viability`, and `armiger_survival_prob`.
3. **Inspect health and classify on demand (`http://localhost:8096`):**
   - `GET http://localhost:8096/healthz` — returns service status, model variant, catalog/adapter readiness, `active_seq`, `active_bundle_merkle_root`, `model_source`, `last_dht_check_ts`, and `sharing_status`.
   - `POST http://localhost:8096/v1/bitmagnet/classify` — classify a batch (`{"torrents": [...], "applyMutations": false}`) and inspect the resulting predictions, GraphQL mutations, and `/import` `contentType` hints.
   - `POST http://localhost:8096/v1/bitmagnet/repair-tags` — scan Bitmagnet for legacy hash-truncated viability tags and replace them with canonical tags.
   - `POST http://localhost:8096/v1/bitmagnet/repair-content-types` — scan Bitmagnet for `Unknown` (`contentType: null`) torrents already carrying an `armiger-<category>` tag and backfill their `contentType` via Bitmagnet's `/import` endpoint (also runs automatically on connector startup).

---

## 3. Environment variables

### Required vs. optional variables

- **When using [`bitmagnet/docker-compose.yml`](docker-compose.yml):** **all settings are already pre-filled with working defaults** — no required environment variables need to be set before running `docker compose up -d`.
- **When running `bitmagnet_connector.py` against an external Bitmagnet stack:** only **`BITMAGNET_GRAPHQL_URL`** (for polling/tagging) and **`UPSTREAM_TORZNAB_URL`** (for the Torznab proxy) are **required** if Bitmagnet is not at `http://localhost:3333`. All other variables are optional.

### Stack, ports, and Bitmagnet settings (`bitmagnet/docker-compose.yml`)

| Variable / Compose setting | Required / Optional | Compose default | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `POSTGRES_PASSWORD` | **Required** *(pre-filled in Compose)* | `"bitmagnet"` | Any non-empty string | PostgreSQL password shared by `postgres` and `bitmagnet`. |
| `POSTGRES_DB` / `POSTGRES_NAME` | **Required** *(pre-filled in Compose)* | `"bitmagnet"` | Valid PostgreSQL database name | PostgreSQL database name for Bitmagnet. |
| `POSTGRES_USER` | **Required** *(pre-filled in Compose)* | `"postgres"` | Valid PostgreSQL username | PostgreSQL user for Bitmagnet. |
| `DHT_CRAWLER_SCALING_FACTOR` | Optional | `"10"` | Integer `"1"` – `"100"` *(recommended `"5"` – `"30"`)* | Bitmagnet DHT crawler concurrency multiplier. Scale `BITMAGNET_BATCH_SIZE` proportionally if raising this above `"30"`. |
| `TMDB_API_KEY` | Optional | `""` *(empty)* | Valid TMDB v3 API key string, or `""` *(disabled)* | Optional TMDB API key for Bitmagnet's built-in TMDB lookup. |
| `bitmagnet` ports (`ports:`) | Optional | `"3333:3333"`, `"3334:3334/tcp"`, `"3334:3334/udp"` | `"<host_port>:3333"`, `"<host_port>:3334/tcp"` & `/udp` | Host ports mapped to Bitmagnet's Web UI / GraphQL / raw Torznab feed (`3333`) and DHT crawler (`3334`). |
| `armiger-ultra` ports (`ports:`) | Optional | `"8096:8096"`, `"8097:8097"` | `"<host_port>:8096"`, `"<host_port>:8097"` | Host ports mapped to the connector's main HTTP listener (`8096`) and dedicated Torznab proxy (`8097`). |

### Polling and Bitmagnet connection (`bitmagnet_connector.py`)

| Variable | Required / Optional | Default (`docker-compose.yml` / CLI) | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `BITMAGNET_GRAPHQL_URL` | **Required** *(for `both`/`poll`; pre-filled in Compose)* | `http://bitmagnet:3333/graphql` *(Compose)* / `http://localhost:3333/graphql` *(CLI)* | Valid `http://` or `https://` URL ending in `/graphql` | Bitmagnet GraphQL endpoint for search, file-list queries, tag writes, and deletions. |
| `UPSTREAM_TORZNAB_URL` | **Required** *(for `both`/`http`; pre-filled in Compose)* | `http://bitmagnet:3333/torznab` *(Compose)* / `http://localhost:3333/torznab` *(CLI)* | Valid `http://` or `https://` URL ending in `/torznab` | Upstream Bitmagnet Torznab endpoint proxied and filtered by Armiger. |
| `BITMAGNET_IMPORT_URL` | Optional | Derived from `BITMAGNET_GRAPHQL_URL` (`http://bitmagnet:3333/import` / `http://localhost:3333/import`) | Valid `http://` or `https://` URL ending in `/import` | Stock Bitmagnet `/import` endpoint used to write `contentType` hints for torrents left as `Unknown` by Bitmagnet's built-in classifier. |
| `ARMIGER_SYNC_CONTENT_TYPE` | Optional | `"true"` | `"true"`, `"false"` | When `"true"`, writes Armiger's predicted `contentType` (`movie`, `tv_show`, `music`, `ebook`, `audiobook`, `comic`, `software`, `game`, `xxx`) to `BITMAGNET_IMPORT_URL` for `Unknown` (`contentType: null`) torrents and backfills already-tagged `armiger-*` `Unknown` torrents on startup. |
| `ARMIGER_CONNECTOR_MODE` | Optional | `"both"` | `"both"`, `"http"`, `"poll"` | `"both"` runs the background GraphQL poller and HTTP/Torznab listeners. `"http"` runs only HTTP/Torznab listeners. `"poll"` runs only the background poller. |
| `BITMAGNET_POLL_SECONDS` | Optional | `"5"` (`5.0`) | Number `>= 1` *(recommended `"2"` – `"60"` seconds)* | Seconds between polling cycles. |
| `BITMAGNET_BATCH_SIZE` | Optional | `"100"` | Integer `"10"` – `"1000"` *(recommended `"50"` – `"500"` items)* | Maximum untagged Bitmagnet torrents classified per cycle. Pages via `offset`/`hasNextPage` across multi-batch 60-second Bitmagnet `persistTorrents` flushes. |

Stock Bitmagnet flushes newly crawled DHT torrents in batches of up to 1,000 items or every 60 seconds. With `BITMAGNET_POLL_SECONDS="5"` and `BITMAGNET_BATCH_SIZE="100"`, the poller pages past items already tagged earlier in the active flush across consecutive cycles (up to 1,200 torrents/minute). If you raise `DHT_CRAWLER_SCALING_FACTOR` above `"30"` or poll less frequently, increase `BITMAGNET_BATCH_SIZE` so `(60 / BITMAGNET_POLL_SECONDS) * BITMAGNET_BATCH_SIZE` exceeds your crawler's per-minute intake.

### Torznab filtering and index pruning (`bitmagnet_connector.py`)

| Variable | Required / Optional | Default (`docker-compose.yml` / CLI) | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `ARMIGER_REQUIRE_ENGLISH_AUDIO` | Optional | `"false"` | `"true"`, `"false"` | When `"true"`, blocks Torznab search results classified as `non_english` audio (subject to `ARMIGER_ALLOW_ENGLISH_SUBS`). |
| `ARMIGER_ALLOW_ENGLISH_SUBS` | Optional | `"true"` | `"true"`, `"false"` | When `ARMIGER_REQUIRE_ENGLISH_AUDIO="true"`, permits `non_english` audio items if English subtitles (`has_eng_subs`) are detected. |
| `ARMIGER_REJECT_DEAD_SWARMS` | Optional | `"true"` *(`"false"` in `spencernorton/armiger` Compose)* | `"true"`, `"false"` | When `"true"`, excludes results classified as `dead` from Torznab responses without deleting them from Bitmagnet. |
| `ARMIGER_PRUNE_MALWARE` | Optional | `"true"` *(`"false"` in `spencernorton/armiger` Compose)* | `"true"`, `"false"` | When `"true"`, deletes torrents flagged as unsafe (`csam_safe=False`), disguised executables, `junk`, or honeypot traps from Bitmagnet's index via `mutation TorrentDelete`. |
| `ARMIGER_PRUNE_DEAD_SWARMS` | Optional | `"false"` | `"true"`, `"false"` | When `"true"`, deletes torrents classified as `dead` from Bitmagnet's index via `mutation TorrentDelete`. |

### Model, catalog, and container listeners

| Variable | Required / Optional | Default (`docker-compose.yml` / CLI) | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `ARMIGER_VARIANT` | Optional | `"ultra"` | `"ultra"`, `"int8"` *(aliases for `"int8"`: `"cpu"`, `"armiger"`)* | `"ultra"` (FP32 ONNX + Ultra plugins) or `"int8"` (INT8 ONNX + Ultra plugins). |
| `ARMIGER_USE_ZERO_CUE_CATALOG` | Optional | `"true"` | `"true"`, `"false"` | Preloads `validated_zero_cue_catalog.json` at startup (`"false"` defers until first lookup). |
| `ARMIGER_MODEL_DIR` | Optional | `"/data/armiger/model"` *(Compose)* / `"."` *(CLI)* | Valid container/host directory path | Directory containing the model bundle. |
| `ARMIGER_AUTO_DOWNLOAD` | Optional | `"true"` | `"true"`, `"false"` | Automatically downloads and verifies missing model files on startup. |
| `ARMIGER_HF_REPO` | Optional | `"NorviTech/armiger-ultra"` | Hugging Face `<org>/<repo>` ID | Hugging Face repository ID used when bootstrapping via the standalone legacy loader. |
| `ARMIGER_CONNECTOR_HOST` | Optional | `"0.0.0.0"` | `"0.0.0.0"`, `"127.0.0.1"`, or valid interface IP | Bind address for HTTP and Torznab listeners (fallback alias: `ARMIGER_BRIDGE_HOST`). |
| `ARMIGER_CONNECTOR_PORT` | Optional | `"8096"` | Integer `"1"` – `"65535"` | Main HTTP listener port for `/healthz`, `/v1/bitmagnet/classify`, and `/torznab` (fallback alias: `ARMIGER_BRIDGE_PORT`). |
| `ARMIGER_PROXY_PORT` | Optional | `"8097"` | Integer `"1"` – `"65535"` | Dedicated Torznab proxy port (`/torznab` and `/api`). |
| `ARMIGER_ENABLE_TORZNAB_PROXY` | Optional | `"true"` | `"true"`, `"false"` | Starts the dedicated `ARMIGER_PROXY_PORT` listener in `"both"` or `"http"` mode. |

### Automatic model updates (BEP 44/46) and optional telemetry

Armiger Ultra automatically checks for newer signed model/catalog bundles in the background, verifies their cryptographic signatures and 500-item canary regression suite, and hot-swaps them without restarting the container. **All release signing keys, DHT bootstrap routers, and Hugging Face HTTPS fallback mirrors are already built in out of the box** — you only need the 5 settings below:

| Variable | Required / Optional | Default (`docker-compose.yml` / CLI) | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `ARMIGER_DHT_AUTO_UPDATE` | Optional | `"true"` | `"true"`, `"false"` *(also accepts `"1"`, `"0"`, `"off"`)* | Enables background BEP 44/46 polling for newer signed model/catalog releases, 500-item canary verification, and atomic hot-swap into `ARMIGER_RUNTIME_CACHE_DIR`. |
| `ARMIGER_DHT_POLL_INTERVAL_SEC` | Optional | `"14400"` *(4 hours)* | Number `>= 60` *(recommended `"3600"` – `"86400"` seconds)* | Seconds between background BEP 44/46 DHT release checks (`"3600"` = 1 hour, `"14400"` = 4 hours, `"86400"` = 24 hours). |
| `ARMIGER_DHT_SHARE_OPT_IN` | Optional | `"false"` | `"true"`, `"false"` *(also accepts `"1"`, `"0"`, `"yes"`, `"no"`)* | Strict opt-in switch (`"true"` to enable; `"false"` by default). Blocks all outbound contribution packets when `"false"`. |
| `ARMIGER_DHT_RSI_MODE` | Optional | `"pull_only"` | `"pull_only"`, `"off"`, `"anonymous_contribute"` | Distributed update policy: `"pull_only"` (receive verified updates only), `"off"` (disable update engine), or `"anonymous_contribute"` (when `ARMIGER_DHT_SHARE_OPT_IN="true"`). |
| `ARMIGER_RUNTIME_CACHE_DIR` | Optional | `/tmp/armiger-live-models-<uid>` | Writable container directory path | Private (`0700`) writable directory where verified DHT live-update overlays are staged. |

<details>
<summary><strong>Advanced: Pre-populated release channel &amp; mirror defaults (no changes needed for normal use)</strong></summary>

| Variable | Required / Optional | Built-in Default | Valid options / range | What it does |
| --- | --- | --- | --- | --- |
| `ARMIGER_RELEASE_PUBKEY_HEX` | Optional *(pre-populated)* | `3599362bd52dad8b89398b904d3081c2cb483d13376f7ffb49f3748208731e76` | 64-character hex Ed25519 public key | Official Ed25519 public key verified on BEP 44/46 mutable release pointers. |
| `ARMIGER_RELEASE_SALT` | Optional *(pre-populated)* | `armiger:weights:ultra` *(or `armiger:weights:int8`)* | Non-empty string (`"armiger:weights:ultra"`, `"armiger:weights:int8"`) | BEP 44 channel salt used with `ARMIGER_RELEASE_PUBKEY_HEX` to derive the DHT lookup target. |
| `ARMIGER_DHT_BOOTSTRAP_NODES` | Optional *(pre-populated)* | `router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881` | Comma-separated `host:port` list | Public Mainline DHT bootstrap routers queried during release discovery. |
| `ARMIGER_WEBSEED_URLS` | Optional *(pre-populated)* | `https://huggingface.co/NorviTech/armiger-ultra/resolve/main` | Comma-separated `https://` base URLs | Official BEP 19 HTTPS fallback mirror used when direct BitTorrent peer-wire transfer is blocked. |

</details>
