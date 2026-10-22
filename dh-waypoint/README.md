# DH-Waypoint `processorapi/v1` Turnkey Stack (`spencernorton/DH-Waypoint`)

Turnkey external processor service implementing the canonical `dh-waypoint.processor.v1` contract (`GET /v1/manifest`, `GET /v1/status`, `POST /v1/process`, `POST /v1/admit`, `POST /v1/classify`, `POST /v1/feedback`, `GET /healthz`) for [`spencernorton/DH-Waypoint`](https://github.com/spencernorton/DH-Waypoint).

## Quickstart

```bash
export POSTGRES_PASSWORD="change_me"
docker compose -f dh-waypoint/docker-compose.yml up -d
```
