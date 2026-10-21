# Rats Search Connector (`librats/rats-search`)

Minimal, zero-modification connector integrating [`Armiger`](https://huggingface.co/NorviTech/armiger) (`8.3 MB` CPU INT8) or [`Armiger Ultra`](https://huggingface.co/NorviTech/armiger-ultra) (`32.3 MB` FP32 / `8.3 MB` INT8 + 6 plugins) with **stock upstream [`librats/rats-search`](https://github.com/librats/rats-search)** (`v2.3.5+`).

## How It Works

Stock `rats-search` includes a built-in REST API (`httpPort` default `8095`), WebSocket event server (`httpPort + 1` default `8096`), and an embedded Manticore RT index (`127.0.0.1:9306`). While `rats-search` defines 9 granular `ContentCategory` IDs (`Movie=1`, `Series=2`, `Documentary=3`, `Anime=4`, `Music=5`, `Ebook=6`, `Comics=7`, `Software=8`, `Game=9`) in `src/domain/content.h` and creates the `contentCategory` column in Manticore, its built-in heuristic classifier leaves `1..9` unpopulated (`0 = Unknown`).

`rats_search_connector.py` upgrades a stock `rats-search` instance without modifying a single line of C++ and requires **zero external CLI binaries or pip dependencies**:

1. **Polls `/api/search.recent` or Processes WebSocket `torrentIndexed` / `filesReady` Events:**
   Fetches newly crawled torrents and enriches multi-file releases via `/api/torrent.get?hash=<hash>&files=true` (`files_list`).
2. **Runs Vectorized Batch Classification:**
   Evaluates all 8 Armiger decision heads plus disguised-executable and honeypot guards in a single batch call.
3. **Purges Malware Lures & Honeypot Traps via `/api/torrent.remove`:**
   Calls `GET /api/torrent.remove?hashes=[...]` on `rats-search`'s REST API so `TorrentRepository::remove()` deletes blocked torrents from both `torrents` and `files` Manticore tables while keeping `rats-search`'s in-memory `Statistics` counters synchronized.
4. **Populates `contentType` & `contentCategory` via `O(1)` Docid Manticore Updates:**
   Computes the exact 63-bit deterministic Manticore row ID (`rowIdFromHash` from `src/data/row_id.cpp`), verifies `SELECT hash FROM torrents WHERE id = <rowId> LIMIT 1` to guard against 63-bit prefix collisions, and executes `UPDATE torrents SET contentType = ..., contentCategory = ... WHERE id = <rowId>` over a built-in pure-Python `mysql41` wire-protocol client (`127.0.0.1:9306`).

## Prerequisites in `rats.json`

Ensure `restApiEnabled` is enabled in your `rats-search` configuration (`rats.json`):

```json
{
  "restApiEnabled": true,
  "httpPort": 8095
}
```

## Quickstart

### 1. Standalone CLI Poller

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
export RATS_REST_URL=http://127.0.0.1:8095
python3 rats-search/rats_search_connector.py
```

### 2. Docker Compose

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
docker compose -f rats-search/docker-compose.yml up -d
```
