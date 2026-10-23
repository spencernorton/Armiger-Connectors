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
