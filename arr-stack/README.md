# Torznab Pre-Grab Proxy & `*Arr`/qBittorrent Post-Import Webhook Stack

Turnkey services connecting [`Armiger Ultra`](https://huggingface.co/NorviTech/armiger-ultra) (`v3.0.0`) to `Prowlarr`, `Sonarr`, `Radarr`, `Lidarr`, `Readarr`, and `qBittorrent`:

- **[`armiger_torznab_proxy.py`](armiger_torznab_proxy.py) (`:8097`)** — Sits between upstream Torznab indexers and `*Arr` media managers; blocks disguised malware lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`), fake-seeder honeypots, dead swarms, and unrequested foreign-language audio before grab.
- **[`armiger_arr_webhook_receiver.py`](armiger_arr_webhook_receiver.py) (`:8098`)** — Receives post-import webhooks from Sonarr (`/webhooks/sonarr`), Radarr (`/webhooks/radarr`), Lidarr (`/webhooks/lidarr`), Readarr (`/webhooks/readarr`), and qBittorrent (`/webhooks/qbittorrent`), running `ffprobe` stream verification for local conformal calibration and opt-in anonymous BEP-44/46 feedback.

## Quickstart

```bash
export UPSTREAM_TORZNAB_URL="http://prowlarr:9696/1/api"
docker compose -f arr-stack/docker-compose.yml up -d
```
