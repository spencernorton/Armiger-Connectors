# Armiger Connectors

Python sidecars connecting [Armiger](https://github.com/spencernorton/armiger)
to stock Bitmagnet, Rats Search, and the *Arr stack.

| Integration | What it does | Setup |
| --- | --- | --- |
| Bitmagnet | Classifies torrents, batches GraphQL tags/deletions, filters Torznab results | [Guide](bitmagnet/README.md) |
| Rats Search | Classifies via REST/WebSocket events and updates Manticore categories | [Guide](rats-search/README.md) |
| *Arr / qBittorrent | Filters Torznab results and receives post-import feedback | [Guide](arr-stack/README.md) |

## Quickstart: Bitmagnet + Armiger

```bash
curl -fsSLO https://raw.githubusercontent.com/spencernorton/Armiger-Connectors/main/bitmagnet/docker-compose.yml
export POSTGRES_PASSWORD="change_me"
docker compose up -d
```

Open Bitmagnet at `http://localhost:3333`, check the connector at
`http://localhost:8096/healthz`, and use `http://localhost:8097/torznab` as the
filtered indexer endpoint. Compose downloads and caches model files in a named
volume. See each guide for configuration and existing installations.

[DH-Waypoint](https://github.com/spencernorton/DH-Waypoint) has its own Armiger
integration.

## Development

Requires Python 3.10+. Tests use synthetic predictions and local HTTP/wire servers;
they do not download models or need a running indexer.

```bash
python -m pip install pytest
python -m pytest -q
```

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Apache-2.0](LICENSE) · [Notice](NOTICE)
