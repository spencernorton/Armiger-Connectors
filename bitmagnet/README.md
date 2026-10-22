# Bitmagnet + Armiger Ultra Turnkey Connector (`bitmagnet-io/bitmagnet`)

Zero-modification, 100% turnkey integration connecting [`Armiger Ultra`](https://huggingface.co/NorviTech/armiger-ultra) (`v3.0.0`, 9-head `31.9 MB` FP32 / `8.2 MB` INT8 + 6 plugins) or [`Armiger`](https://huggingface.co/NorviTech/armiger) with **stock upstream [`bitmagnet-io/bitmagnet`](https://github.com/bitmagnet-io/bitmagnet)**.

## What `docker-compose.yml` Launches

[`docker-compose.yml`](docker-compose.yml) starts PostgreSQL 16 (`postgres`), stock `bitmagnet` (`:3333` Web UI / GraphQL / Torznab), and `armiger-ultra` (`:8096` GraphQL poller + HTTP bridge and `:8097` Torznab pre-grab filter proxy) in a single `docker compose up -d`:

1. **Automatic Model & Plugin Bootstrap (`NorviTech/armiger-ultra`):**
   On first container start, `armiger-ultra` automatically downloads and caches `NorviTech/armiger-ultra` (`armiger_ultra.fp32.onnx`, `armiger.int8.onnx`, `swarm_adapter.pt`, `validated_zero_cue_catalog.json`, `calibration.json`, and the 6 plugin modules) into the persistent `armiger_data` Docker volume. No manual `git clone` of `armiger` is required.
2. **Continuous Bitmagnet GraphQL Poller & Batch Classifier (`ARMIGER_CONNECTOR_MODE=both`):**
   - Queries untagged newly crawled torrents (`torrentContent.search`) and batch-fetches file trees (`torrent.files`).
   - Evaluates all 9 Armiger neural heads (`category`, `adult`, `junk`, `anime`, `lang`, `title_language`, `english_subs`, `owner_scope`, `span_bio`) plus all 6 Armiger Ultra plugins (`SwarmSurvivalNet`, `ZeroCueCatalogResolver`, `AudioLanguageGuard`, `MondrianConformalGate`, `DHTRSIEngine`).
   - Immediately purges disguised executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`, `.bat`, `.ps1`), safety-gate violations, and fake-seeder honeypot traps via `mutation TorrentDelete($infoHashes: [Hash20!]!)`.
   - Applies grouped kebab-case tags (`armiger-<category>`, `armiger-lang-<lang>`, `armiger-title-<lang>`, `armiger-viability-<tier>`, `armiger-anime`, `armiger-eng-subs`, matching Bitmagnet's `^[a-z0-9]+(-[a-z0-9]+)*$` tag constraint) via `mutation TorrentPutTags($infoHashes: [Hash20!]!, $tagNames: [String!]!)`.
3. **Built-In Torznab Pre-Grab Proxy (`:8097` & `:8096/torznab`):**
   Point Prowlarr, Sonarr, Radarr, Lidarr, or Readarr at `http://localhost:8097/torznab` (instead of `http://localhost:3333/torznab`) to filter out malware lures, dead swarms, and unrequested foreign-language audio before any torrent is grabbed.
4. **HTTP Classification & Sync API (`:8096`):**
   - `GET http://localhost:8096/healthz` — service, model variant, and mode status
   - `POST http://localhost:8096/v1/bitmagnet/classify` — classify and optionally apply mutations for a batch of torrents
   - `POST http://localhost:8096/v1/bitmagnet/sync` — trigger an on-demand Bitmagnet GraphQL poll + tag/delete cycle

## Turnkey Quickstart

### Option A: Single-File `docker-compose.yml` (Zero Local Clones Required)

```bash
curl -fsSLO https://raw.githubusercontent.com/spencernorton/Armiger-Connectors/main/bitmagnet/docker-compose.yml
export POSTGRES_PASSWORD="change_me"
docker compose up -d
```

### Option B: From a Clone of `Armiger-Connectors`

```bash
export POSTGRES_PASSWORD="change_me"
docker compose -f bitmagnet/docker-compose.yml up -d
```

### Option C: Standalone Python CLI (Against an Existing Bitmagnet Instance)

```bash
pip install numpy onnxruntime cryptography huggingface_hub
export BITMAGNET_GRAPHQL_URL=http://localhost:3333/graphql
python3 bitmagnet/bitmagnet_connector.py
```
