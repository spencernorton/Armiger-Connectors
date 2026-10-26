# *Arr / qBittorrent

- [Torznab proxy](armiger_torznab_proxy.py), `:8097`: filters upstream results
  before a grab. Set `UPSTREAM_TORZNAB_URL` to a reachable Prowlarr/indexer URL.
- [Webhook receiver](armiger_arr_webhook_receiver.py), `:8098`: accepts import
  feedback at `/webhooks/sonarr`, `/webhooks/radarr`, `/webhooks/lidarr`,
  `/webhooks/readarr`, and `/webhooks/qbittorrent`. Optional `ffprobe` verification
  requires imported files to be accessible inside the container.

```bash
export UPSTREAM_TORZNAB_URL="http://prowlarr:9696/1/api"
docker compose -f arr-stack/docker-compose.yml up -d
```

[Compose](docker-compose.yml) lists settings and volume mounts. Models are cached
in `armiger_data`; sharing remains opt-in. Both services expose `GET /healthz`.
