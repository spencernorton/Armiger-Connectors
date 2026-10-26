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
