# Rats Search

Connects to stock [Rats Search](https://github.com/librats/rats-search) through
REST (`:8095`), WebSocket events (`:8096`), and Manticore (`:9306`). Armiger
populates content categories; removals use the REST API to keep upstream
statistics synchronized. Manticore updates verify the hash at each calculated
row ID before writing.

Enable the REST API in `rats.json`:

```json
{"restApiEnabled": true, "httpPort": 8095}
```

Run the sidecar alongside an existing Rats Search instance:

```bash
docker compose -f rats-search/docker-compose.yml up -d
```

The sidecar uses host networking to reach Rats Search and its local Manticore
listener. Models are downloaded on first use and cached in `armiger_data`.

For a local Python process, install
[Armiger](https://github.com/spencernorton/armiger#quickstart), then run:

```bash
export RATS_REST_URL=http://127.0.0.1:8095
python3 rats-search/rats_search_connector.py
```

Use `GET /healthz` or `POST /v1/rats-search/classify` on `:8097`.
[Compose](docker-compose.yml) lists settings, including polling frequency,
Manticore access, and optional pruning. The built-in Manticore client requires
no MySQL package.
