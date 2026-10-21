# Bitmagnet Connector (`bitmagnet-io/bitmagnet`)

Minimal, zero-modification connector integrating [`Armiger`](https://huggingface.co/NorviTech/armiger) (`8.3 MB` CPU INT8) or [`Armiger Ultra`](https://huggingface.co/NorviTech/armiger-ultra) (`32.3 MB` FP32 / `8.3 MB` INT8 + 6 plugins) with **stock upstream [`bitmagnet-io/bitmagnet`](https://github.com/bitmagnet-io/bitmagnet)**.

## How It Works

Stock `bitmagnet` exposes its GraphQL API at `http://localhost:3333/graphql`. `bitmagnet_connector.py` runs alongside `bitmagnet` with zero changes to upstream Go code:

1. **Queries Untagged Recent Torrents (`torrentContent.search`):**
   Polls newly indexed torrents ordered by `updated_at` descending and skips any infohash that already carries an `armiger:*` tag.
2. **Batch-Fetches File Trees (`torrent.files`):**
   Fetches file paths and byte sizes for untagged multi-file torrents in a single GraphQL request.
3. **Runs Vectorized Batch Classification:**
   Classifies the entire batch in one pass (`0.21 ms/item` on RTX GPU or `2.44 ms/item` on CPU INT8).
4. **Deletes Disguised Malware & Honeypots (`torrent.delete`):**
   Immediately prunes double-extension executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`), safety-gate violations, and fake-seeder honeypot traps in a single batched `mutation TorrentDelete($infoHashes: [Hash20!]!)` call.
5. **Groups Tag Mutations (`torrent.putTags`):**
   Groups surviving infohashes that share the same tag set (`armiger:<category>`, `armiger:lang:<lang>`, `armiger:viability:<tier>`, `armiger:anime`, `armiger:eng_subs`) into minimal batched `mutation TorrentPutTags($infoHashes: [Hash20!]!, $tagNames: [String!]!)` calls.

## Quickstart

### 1. Standalone CLI Poller

```bash
export ARMIGER_MODEL_DIR=/path/to/armiger
export BITMAGNET_GRAPHQL_URL=http://localhost:3333/graphql
python3 bitmagnet/bitmagnet_connector.py
```

### 2. Docker Compose Stack

```bash
export POSTGRES_PASSWORD="change_me"
export ARMIGER_MODEL_DIR=/path/to/armiger
docker compose -f bitmagnet/docker-compose.yml up -d
```
