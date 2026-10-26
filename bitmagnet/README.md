# Bitmagnet

The connector polls stock [Bitmagnet](https://github.com/bitmagnet-io/bitmagnet),
batch-fetches file lists, and groups Armiger results into GraphQL tag and deletion
mutations. It also exposes a Torznab filter and HTTP classification bridge.

From a clone of this repository:

```bash
export POSTGRES_PASSWORD="change_me"
docker compose -f bitmagnet/docker-compose.yml up -d
```

This starts PostgreSQL, Bitmagnet (`:3333`), the connector (`:8096`) and the
Torznab proxy (`:8097`). Models are downloaded on first use and cached in
`armiger_data`.

For an existing Bitmagnet installation, install
[Armiger](https://github.com/spencernorton/armiger#quickstart), then run:

```bash
export BITMAGNET_GRAPHQL_URL=http://localhost:3333/graphql
python3 bitmagnet/bitmagnet_connector.py
```

Use `GET /healthz` or `POST /v1/bitmagnet/classify` on `:8096`.
Configure your indexer client with
`http://localhost:8097/torznab` for filtered results.

[Compose](docker-compose.yml) lists environment settings. Classification applies
`armiger-*` tags within Bitmagnet's 30-character limit. Malware pruning defaults
on; dead-swarm deletion defaults off. Torznab dead-swarm filtering is controlled
separately by `ARMIGER_REJECT_DEAD_SWARMS` (default `true`).

Stock Bitmagnet flushes newly crawled DHT torrents in batches of up to 1,000 items or every 60 seconds. The poller (`BITMAGNET_POLL_SECONDS=5`, `BITMAGNET_BATCH_SIZE=100`) pages past items already tagged earlier in the active flush until the full wave is classified.
