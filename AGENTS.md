# Armiger Connectors

Adapters connecting Armiger to Bitmagnet, Rats Search and other indexers.

- Tests: `python -m pytest -q`. The required `test` also includes native amd64/arm64 real-container acceptance against the released Armiger package and stock upstreams; unit or mock checks alone are insufficient.
- Keep legacy model bundles compatible while preferring installed Armiger code and verified data files.
- Use `codex/`, `claude/` or `spencer/` branch prefixes.
- Read [the journal](AGENTS/journal.md) for recent changes. Private operating context lives in `shared-ops/projects/status/bitagent.md`.
