## 2026-10-28 — Keep prebuilt-image instructions in the canonical setup guide

- **Branch:** codex/canonical-image-guide
- **What changed:** Remove the duplicated3.3.4 image pin from the Bitmagnet overview and direct prebuilt-container users to the current Armiger setup guide and configuration reference. The old pin retained the non-root directory-permission defect fixed in3.4.1.
- **Validation:** Documentation-only change; connector test suite and protected CI required before merge.
- **Deployment:** None. Connector source and model data are unchanged.

## 2026-10-27 — NorviTechGH — Canonical Bitmagnet viability tags and automatic legacy tag repair (v1.2.5)

- **Branch:** fix/bitmagnet-viability-tags-and-repair
- **What changed:** Map Bitmagnet viability tier tags to human-readable canonical names within Bitmagnet's 30-character limit (`armiger-viability-alive-viable`, `armiger-viability-degraded`, `armiger-viability-stalled`, `armiger-viability-dead`, `armiger-viability-unknown-cold`). Add `BitmagnetConnector.build_delete_tags_mutation`, `BitmagnetConnector.repair_legacy_viability_tags`, `POST /v1/bitmagnet/repair-tags`, and opportunistic in-page repair in `sync_once` so existing instances automatically replace and delete legacy hash-truncated tags (`armiger-viability-dea-c24281f7`, `armiger-viability-deg-7c170f8b`, `armiger-viability-sus-cdc9d2c6`). Refresh cached `/data/armiger/bitmagnet_connector.py` on container start in `bitmagnet/docker-compose.yml` and bump package version to `1.2.5`.
- **Why:** Fix `spencernorton/armiger#42` where 33-character viability tier names were hash-truncated to `armiger-viability-dea-c24281f7`, `armiger-viability-deg-7c170f8b`, and `armiger-viability-sus-cdc9d2c6`, and automatically migrate existing Bitmagnet databases.
- **Validation:** 45 tests pass (`pytest -v`), `docker compose -f bitmagnet/docker-compose.yml config --quiet`, and `git diff --check`.
- **Deployment:** None; pinned in `spencernorton/armiger` (`v3.3.4`).
- **Next step:** Tag `v1.2.5` after merge and pin in `spencernorton/armiger`.
- **Status file updated:** Yes.

## 2026-10-27 — Release v1.2.4: Sync Bitmagnet guide image tag to armiger:3.3.3 and publish GitHub Release

- **Branch:** release/v1.2.4-sync-armiger-333
- **What changed:** Bump `pyproject.toml` version to `1.2.4` and update `bitmagnet/README.md` image reference to `ghcr.io/spencernorton/armiger:3.3.3`.
- **Validation:** `pytest -q` and `git diff --check`.
- **Follow-ups:** Publish `v1.2.4` GitHub Release.

## 2026-10-26 — Document Bitmagnet setup, usage, and all environment variables

- **Branch:** docs/bitmagnet-env-and-usage-guide
- **What changed:** Expand `bitmagnet/README.md` with step-by-step setup (turnkey Compose and existing Bitmagnet instance), usage instructions for `armiger-*` tags in Bitmagnet (`:3333`), Prowlarr/`*Arr` Torznab proxy filtering (`:8097/torznab`), and HTTP health/classification endpoints (`:8096`), plus complete environment variable tables covering stack/Bitmagnet settings, polling and `DHT_CRAWLER_SCALING_FACTOR` scaling, Torznab filtering, index pruning, model/catalog/listener settings, and BEP 44/46 auto-update / opt-in sharing variables. Expose `DHT_CRAWLER_SCALING_FACTOR`, `ARMIGER_ENABLE_TORZNAB_PROXY`, `ARMIGER_DHT_AUTO_UPDATE`, `ARMIGER_DHT_POLL_INTERVAL_SEC`, and `ARMIGER_DHT_SHARE_OPT_IN` in `bitmagnet/docker-compose.yml`.
- **Validation:** Ran `pytest -q` and `docker compose -f bitmagnet/docker-compose.yml config --quiet`.
- **Deployment:** None; documentation and Compose interpolation defaults only (`bitmagnet_connector.py` bytes unchanged).
- **Next step:** Merge after green required checks.
- **Status file updated:** Yes.

## 2026-10-26 — Clarify Bitmagnet env variable options, pre-populated defaults, and literal Compose values

### Summary
- Replaced `${VAR:-default}` variable interpolation in `bitmagnet/docker-compose.yml` with literal default values so the file can be copy-pasted and started directly with `docker compose up -d`.
- Added `Required / Optional` and `Valid options / range` columns to all environment variable tables in `bitmagnet/README.md`, and simplified the automatic model updates section with pre-populated defaults.

## 2026-10-26 — NorviTechGH — Paginate Bitmagnet poller across multi-batch crawl flushes

- **Branch:** fix/bitmagnet-poller-pagination
- **What changed:** Update `BitmagnetConnector.sync_once` to request `hasNextPage` and advance `offset` past items already tagged earlier in the current flush (`_wave_seen`) until `limit` untagged items are collected or the prior tagged frontier is reached. Exclude deleted hashes from `_wave_seen` so row shifts after `torrent.delete` never skip untagged entries across page boundaries.
- **Why:** Stock Bitmagnet flushes crawled DHT torrents in bursts of up to 1,000 items or every 60 seconds, and `torrent.putTags` leaves newly tagged rows at the top of `updated_at DESC`. Without pagination, subsequent 5-second polls re-fetched `offset=0` and left items beyond the first `BITMAGNET_BATCH_SIZE` window untagged (`spencernorton/armiger#37`).
- **Validation:** 43 tests pass (`python3 -m pytest -v`), including multi-poll 350-item flush pagination and deletion-shift regression coverage.
- **Deployment:** None; pinned in `spencernorton/armiger`.
- **Next step:** Tag `v1.2.3` and pin in `spencernorton/armiger`.
- **Status file updated:** Yes.

## 2026-10-26 — codex — Consolidate shared runtime and simplify setup guides

- **Branch:** codex/slim-connectors
- **What changed:** Share classifier dispatch, seen-cache handling, health reporting, JSON responses, Torznab parsing and feed filtering. Both Arr services use the common installed/legacy model loader; Compose no longer duplicates model downloads. Shorten repeated setup prose while retaining all three integrations.
- **Comparison:** ApolloF/main is upstream v1.1.0 (395e4c8), with no unique commits. Preserve subsequent upstream fixes.
- **Validation:** 42 tests; 2,000 prediction cases match v1.2.1; Ruff, Compose configuration, embedded bootstrap syntax and Python 3.10 syntax pass.
- **Deployment:** None. Source version 1.2.2; model artifacts and inference are unchanged.
- **Next step:** Complete protected CI and release.
- **Status file updated:** Companion status update prepared.

## 2026-10-26 — NorviTechGH — Wire DHT auto-update observability and environment-resolved opt-in sharing

- **Branch:** feat/dht-autoupdate-and-sharing-status
- **What changed:** Pass `opt_in_sharing=None` and environment-resolved `rsi_mode` in `bitmagnet_connector.py` and `rats_search_connector.py` so `ARMIGER_DHT_SHARE_OPT_IN` resolves from the environment (strictly defaulting to `False` / `pull_only`), and expose `active_seq`, `active_bundle_merkle_root`, `model_source`, `last_dht_check_ts`, and `sharing_status()` on `/healthz`.
- **Why:** Enable runtime observability of BEP-44/46 background auto-updates and environment-controlled opt-in contribution telemetry across standalone and connector deployments.
- **Validation:** Connector suite passes (14 tests).
- **Deployment:** None; pinned in the Armiger container image.
- **Next step:** Pin merged connector revision in `spencernorton/Armiger`.
- **Status file updated:** Yes.

## 2026-10-24 — NorviTechGH — Remove the DH-Waypoint connector

- **Branch:** remove-dh-waypoint-connector
- **What changed:** Removed `dh-waypoint/` (processor, plugin file, Compose file and README) and the Torznab proxy's DH-Waypoint default upstream; the proxy now defaults to the Prowlarr URL its Compose file already sets. The README links to DH-Waypoint instead.
- **Why:** The connector could not register with DH-Waypoint, which has its own Armiger Ultra integration.
- **Validation:** Connector suite passes (14 tests).
- **Deployment:** None.
- **Next step:** None.
- **Status file updated:** No.

## 2026-10-23 — codex — Bitmagnet tag length compatibility

- **Branch:** codex/bitmagnet-tag-limit
- **What changed:** Bound normalized tags to stock Bitmagnet's 30-character limit, retaining a readable prefix and stable hash suffix for distinct long labels. Short existing tags stay unchanged.
- **Why:** Fresh-stack verification encountered `armiger-viability-dead-known-zero` (31 characters), which rejected a poll batch.
- **Validation:** Regression covers the observed label, stable/idempotent shortening, distinct suffixes and GraphQL mutation output; complete connector suite runs before merge.
- **Deployment:** None; the Armiger image will pin the merged connector.
- **Next step:** Complete protected checks and verify against disposable stock Bitmagnet.
- **Status file updated:** Container-release tracker records this acceptance fix.

## 2026-10-23 — codex — installed Armiger compatibility

- **Branch:** codex/installed-armiger-loader
- **What changed:** Prefer the installed Armiger package and its verified model downloader; preserve the legacy bundle loader for existing users. Model caches do not enter the Python search path when using the package.
- **Validation:** Connector suite and model-loading regression.
- **Deployment:** None; reusable source for the Armiger Compose examples.
- **Next step:** Verify the pinned source in both Compose stacks.
- **Status file updated:** Pending final handoff.
