<h1 align="center">Armiger-Connectors</h1>

<p align="center">
  <strong>Minimal, high-efficiency connectors for stock upstream Bitmagnet and Rats Search.</strong><br>
  Drop-in Python sidecars that connect <a href="https://github.com/spencernorton/armiger">Armiger</a> and <a href="https://huggingface.co/NorviTech/armiger-ultra">Armiger Ultra</a> to unmodified <a href="https://github.com/bitmagnet-io/bitmagnet">bitmagnet-io/bitmagnet</a> and <a href="https://github.com/librats/rats-search">librats/rats-search</a> deployments with zero upstream code changes.
</p>

<p align="center">
  <a href="https://norvitech.com"><img alt="NorviTech Suite" src="https://img.shields.io/badge/NorviTech-Suite-FD8024.svg"></a>
  <a href="https://github.com/spencernorton/armiger"><img alt="Armiger Core" src="https://img.shields.io/badge/Armiger-Core_%26_6_Plugins-0284C7.svg"></a>
  <a href="https://huggingface.co/NorviTech/armiger-ultra"><img alt="Hugging Face" src="https://img.shields.io/badge/Hugging_Face-NorviTech-FFD21E.svg"></a>
  <a href="LICENSE"><img alt="Licence Apache-2.0" src="https://img.shields.io/badge/licence-Apache--2.0-blue.svg"></a>
</p>

---

## Overview

`Armiger-Connectors` is a standalone companion repository to [`spencernorton/armiger`](https://github.com/spencernorton/armiger). While the core `armiger` repository and [`NorviTech`](https://huggingface.co/NorviTech) Hugging Face packages contain strictly the 8-head neural classifier (`8.3 MB` CPU INT8 / `32.3 MB` FP32) and the 6 official plugins, this repository provides lightweight external connectors for stock upstream DHT indexers:

1. **[`bitmagnet/` (`bitmagnet/bitmagnet_connector.py`)](bitmagnet/README.md)** — Connects to stock upstream [`bitmagnet-io/bitmagnet`](https://github.com/bitmagnet-io/bitmagnet) via its GraphQL API (`http://localhost:3333/graphql`):
   - Polls newly crawled untagged torrents (`torrentContent.search`) and batch-fetches their file trees in a single `torrent.files` query.
   - Runs a single vectorized Armiger batch inference pass (`0.21 ms/item` GPU or `2.44 ms/item` CPU INT8).
   - Prunes disguised executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`), safety-gate violations, and fake-seeder honeypot traps in one batched `torrent.delete(infoHashes: $infoHashes)` mutation.
   - Groups surviving torrents that share the same tag set (`armiger:<category>`, `armiger:lang:<lang>`, `armiger:viability:<tier>`, `armiger:anime`, `armiger:eng_subs`) into minimal batched `torrent.putTags(infoHashes: $infoHashes, tagNames: $tagNames)` mutations.

2. **[`rats-search/` (`rats-search/rats_search_connector.py`)](rats-search/README.md)** — Connects to stock upstream [`librats/rats-search`](https://github.com/librats/rats-search) (`v2.3.5+`) via its built-in REST API (`:8095`), WebSocket push stream (`:8096`), and local Manticore RT index (`127.0.0.1:9306`):
   - Polls `/api/search.recent` or processes WebSocket `torrentIndexed` / `filesReady` events, enriching multi-file items via `/api/torrent.get?hash=<hash>&files=true`.
   - Purges disguised malware lures (`.mkv.exe`, `.scr`, `.lnk`), safety violations, and honeypot traps via `GET /api/torrent.remove?hashes=[...]` so `TorrentRepository`'s in-memory `Statistics` counters stay synchronized.
   - Activates `rats-search`'s 9 granular `ContentCategory` IDs (`Movie=1`, `Series=2`, `Documentary=3`, `Anime=4`, `Music=5`, `Ebook=6`, `Comics=7`, `Software=8`, `Game=9`) and `XXX=100` in the Manticore `torrents` RT table using `rats-search`'s deterministic 63-bit `rowIdFromHash` docid lookup (`O(1)`) with collision verification.

---

## Quickstart

### 1. Stock Upstream `bitmagnet`

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
export BITMAGNET_GRAPHQL_URL=http://localhost:3333/graphql
python3 bitmagnet/bitmagnet_connector.py
```

Or launch the full Docker Compose stack in [`bitmagnet/docker-compose.yml`](bitmagnet/docker-compose.yml):

```bash
export POSTGRES_PASSWORD="change_me"
export ARMIGER_MODEL_DIR=/path/to/armiger
docker compose -f bitmagnet/docker-compose.yml up -d
```

### 2. Stock Upstream `rats-search`

Enable `"restApiEnabled": true` and `"httpPort": 8095` in `rats.json`, then run:

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
export RATS_REST_URL=http://127.0.0.1:8095
python3 rats-search/rats_search_connector.py
```

Or run via Docker Compose using [`rats-search/docker-compose.yml`](rats-search/docker-compose.yml):

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
docker compose -f rats-search/docker-compose.yml up -d
```

---

## Running Tests

```bash
pytest -v
```

- **License:** [Apache License, Version 2.0](LICENSE) · [`NOTICE`](NOTICE) · [`SECURITY.md`](SECURITY.md)
