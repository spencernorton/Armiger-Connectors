<h1 align="center">Armiger-Connectors</h1>

<p align="center">
  <strong>Turnkey connectors and Docker Compose stacks for Bitmagnet, Rats Search, DH-Waypoint, and the *Arr ecosystem.</strong><br>
  Drop-in Python sidecars that connect <a href="https://github.com/spencernorton/armiger">Armiger</a> and <a href="https://huggingface.co/NorviTech/armiger-ultra">Armiger Ultra</a> (<code>v3.0.0</code>, 9-head neural core + 6 plugins) to unmodified <a href="https://github.com/bitmagnet-io/bitmagnet">bitmagnet-io/bitmagnet</a>, <a href="https://github.com/librats/rats-search">librats/rats-search</a>, <a href="https://github.com/spencernorton/DH-Waypoint">spencernorton/DH-Waypoint</a>, and <code>Prowlarr</code>/<code>Sonarr</code>/<code>Radarr</code>/<code>Lidarr</code>/<code>Readarr</code>/<code>qBittorrent</code> deployments with zero upstream code changes.
</p>

<p align="center">
  <a href="https://norvitech.com"><img alt="NorviTech Suite" src="https://img.shields.io/badge/NorviTech-Suite-FD8024.svg"></a>
  <a href="https://github.com/spencernorton/armiger"><img alt="Armiger v3.0.0" src="https://img.shields.io/badge/Armiger_v3.0.0-9--Head_%26_6_Plugins-0284C7.svg"></a>
  <a href="https://huggingface.co/NorviTech/armiger-ultra"><img alt="Hugging Face" src="https://img.shields.io/badge/Hugging_Face-NorviTech-FFD21E.svg"></a>
  <a href="LICENSE"><img alt="Licence Apache-2.0" src="https://img.shields.io/badge/licence-Apache--2.0-blue.svg"></a>
</p>

---

## Overview

`Armiger-Connectors` is the official companion repository to [`spencernorton/armiger`](https://github.com/spencernorton/armiger). Every `docker-compose.yml` in this repository is **100% turnkey**: on first startup, each container automatically downloads and caches [`NorviTech/armiger-ultra`](https://huggingface.co/NorviTech/armiger-ultra) (`armiger_ultra.fp32.onnx`, `armiger.int8.onnx`, `swarm_adapter.pt`, `validated_zero_cue_catalog.json`, and all 6 plugin modules) into a persistent Docker volume—no manual local checkout or bind mount is required.

1. **[`bitmagnet/`](bitmagnet/README.md) (`bitmagnet/bitmagnet_connector.py` + `bitmagnet/docker-compose.yml`)** — Connects to stock upstream [`bitmagnet-io/bitmagnet`](https://github.com/bitmagnet-io/bitmagnet) via its GraphQL API (`http://bitmagnet:3333/graphql`) and Torznab endpoint:
   - Polls newly crawled untagged torrents (`torrentContent.search`) and batch-fetches their file trees in a single `torrent.files` query.
   - Evaluates all 9 Armiger neural heads (`category`, `adult`, `junk`, `anime`, `lang`, `title_language`, `english_subs`, `owner_scope`, `span_bio`) plus all 6 Armiger Ultra plugins (`0.22 ms/item` GPU or `2.51 ms/item` CPU INT8).
   - Prunes disguised executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`, `.bat`, `.ps1`), safety-gate violations, and fake-seeder honeypot traps in one batched `torrent.delete(infoHashes: $infoHashes)` mutation.
   - Groups surviving torrents that share the same tag set (`armiger-<category>`, `armiger-lang-<lang>`, `armiger-title-<lang>`, `armiger-viability-<tier>`, `armiger-anime`, `armiger-eng-subs`) into minimal batched `torrent.putTags(infoHashes: $infoHashes, tagNames: $tagNames)` mutations.
   - Exposes a built-in Torznab pre-grab filter proxy on `:8097` (`/torznab`) and an HTTP classification/sync bridge on `:8096` (`/healthz`, `/v1/bitmagnet/classify`, `/v1/bitmagnet/sync`).

2. **[`rats-search/`](rats-search/README.md) (`rats-search/rats_search_connector.py` + `rats-search/docker-compose.yml`)** — Connects to stock upstream [`librats/rats-search`](https://github.com/librats/rats-search) (`v2.3.5+`) via its built-in REST API (`:8095`), WebSocket push stream (`:8096`), and local Manticore RT index (`127.0.0.1:9306`):
   - Polls `/api/search.recent` or processes WebSocket `torrentIndexed` / `filesReady` events, enriching multi-file items via `/api/torrent.get?hash=<hash>&files=true`.
   - Purges disguised malware lures, safety violations, and honeypot traps via `GET /api/torrent.remove?hashes=[...]` so `TorrentRepository`'s in-memory `Statistics` counters stay synchronized.
   - Activates `rats-search`'s 9 granular `ContentCategory` IDs (`Movie=1` .. `Game=9`) and `XXX=100` in the Manticore `torrents` RT table using `rats-search`'s deterministic 63-bit `rowIdFromHash` docid lookup (`O(1)`) with collision verification over a built-in pure-Python `mysql41` wire client.

3. **[`dh-waypoint/`](dh-waypoint/README.md) (`dh-waypoint/armiger_waypoint_processor.py` + `dh-waypoint/07-armiger-ultra.plugin.yml` + `dh-waypoint/docker-compose.yml`)** — Canonical `dh-waypoint.processor.v1` external processor for [`spencernorton/DH-Waypoint`](https://github.com/spencernorton/DH-Waypoint) supporting pre-ingest admission (`/v1/admit`), batch classification (`/v1/classify`), single-job processing (`/v1/process`), and 4-tier feedback (`/v1/feedback`).

4. **[`arr-stack/`](arr-stack/README.md) (`arr-stack/armiger_torznab_proxy.py` + `arr-stack/armiger_arr_webhook_receiver.py` + `arr-stack/docker-compose.yml`)** — Standalone Torznab pre-grab XML admission proxy (`:8097`) and `*Arr`/qBittorrent post-import `ffprobe` ground-truth webhook receiver (`:8098`).

---

## Turnkey Quickstart (`Bitmagnet + Armiger Ultra`)

Run the complete Bitmagnet + Armiger Ultra stack from a single `docker-compose.yml` (or from a clone of this repo):

```bash
curl -fsSLO https://raw.githubusercontent.com/spencernorton/Armiger-Connectors/main/bitmagnet/docker-compose.yml
export POSTGRES_PASSWORD="change_me"
docker compose up -d
```

- **Bitmagnet Web UI & GraphQL:** `http://localhost:3333`
- **Armiger Ultra HTTP & GraphQL Bridge:** `http://localhost:8096/healthz`
- **Armiger-Filtered Torznab Endpoint (for Prowlarr / Sonarr / Radarr):** `http://localhost:8097/torznab`

---

## Running Tests

```bash
pytest -v
```

- **License:** [Apache License, Version 2.0](LICENSE) · [`NOTICE`](NOTICE) · [`SECURITY.md`](SECURITY.md)
