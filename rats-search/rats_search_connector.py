#!/usr/bin/env python3
"""Minimal, high-efficiency Armiger connector for stock upstream `librats/rats-search`.

Integrates with unmodified `librats/rats-search` (`v2.3.5+`) through its built-in
REST API (`http://localhost:8095`), WebSocket broadcast stream (`ws://localhost:8096`),
and embedded Manticore RT index (`127.0.0.1:9306`):
  1. Polls `/api/search.recent` or listens for WebSocket `torrentIndexed` /
     `filesReady` push events, enriching multi-file items via `/api/torrent.get?files=true`.
  2. Runs a single vectorized Armiger batch inference pass (`ArmigerONNXClassifier`
     or `ArmigerUltraPipeline`).
  3. Purges disguised executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`),
     safety-gate violations, and fake-seeder honeypot traps via
     `GET /api/torrent.remove?hashes=[...]` so `TorrentRepository`'s in-memory
     statistics stay synchronized.
  4. Populates `rats-search`'s `contentType` and `contentCategory` (`Movie=1` ..
     `Game=9`, `XXX=100`) columns in the Manticore `torrents` RT table using
     `rats-search`'s deterministic 63-bit `rowIdFromHash` docid lookup (`O(1)`).
"""
from __future__ import annotations

from collections import OrderedDict
from enum import IntEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.parse
import urllib.request

_CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
if str(_CONNECTOR_ROOT / "bitmagnet") not in sys.path:
    sys.path.insert(0, str(_CONNECTOR_ROOT / "bitmagnet"))

from bitmagnet_connector import (  # noqa: E402
    has_disguised_executable,
    load_armiger_classifier,
    normalize_prediction,
)


class RatsContentType(IntEnum):
    """Exact `rats::domain::ContentType` IDs from `src/domain/content.h`."""

    UNKNOWN = 0
    VIDEO = 1
    AUDIO = 2
    BOOKS = 3
    PICTURES = 4
    SOFTWARE = 5
    GAMES = 6
    ARCHIVE = 7
    BAD = 100


class RatsContentCategory(IntEnum):
    """Exact `rats::domain::ContentCategory` IDs from `src/domain/content.h`."""

    UNKNOWN = 0
    MOVIE = 1
    SERIES = 2
    DOCUMENTARY = 3
    ANIME = 4
    MUSIC = 5
    EBOOK = 6
    COMICS = 7
    SOFTWARE = 8
    GAME = 9
    XXX = 100


_DOCU_CUE_RE = re.compile(r"\b(?:documentary|docuseries|docu|bbc|pbs|natgeo|nhk)\b", re.IGNORECASE)
_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def row_id_from_hash(infohash: str) -> int:
    """Exact Python implementation of `rats::data::rowIdFromHash` (`src/data/row_id.cpp`).

    Takes the top 64 bits (first 16 hex characters) of a 40-char BitTorrent v1
    infohash, right-shifts by 1 bit into a positive 63-bit signed integer, and
    nudges `0` to `1` so Manticore never treats `0` as auto-assign.
    """
    clean = infohash.strip()
    if len(clean) < 16:
        return 0
    try:
        top = int(clean[:16], 16)
    except ValueError:
        return 0
    row_id = top >> 1
    return 1 if row_id == 0 else row_id


def map_to_rats_enums(pred: Mapping[str, Any], name: str = "") -> Tuple[RatsContentType, RatsContentCategory]:
    """Map an Armiger prediction to `rats-search` `(ContentType, ContentCategory)` IDs."""
    category = str(pred.get("category") or "unknown")
    adult = str(pred.get("adult") or "non_adult")
    anime = str(pred.get("anime") or "non_anime")

    if category == "xxx" or adult == "adult":
        return (RatsContentType.BAD, RatsContentCategory.XXX)
    if anime == "anime" and category in {"movie", "tv_show"}:
        return (RatsContentType.VIDEO, RatsContentCategory.ANIME)
    if category == "movie":
        if _DOCU_CUE_RE.search(name):
            return (RatsContentType.VIDEO, RatsContentCategory.DOCUMENTARY)
        return (RatsContentType.VIDEO, RatsContentCategory.MOVIE)
    if category == "tv_show":
        return (RatsContentType.VIDEO, RatsContentCategory.SERIES)
    if category in {"music", "audiobook"}:
        return (RatsContentType.AUDIO, RatsContentCategory.MUSIC)
    if category in {"ebook", "course"}:
        return (RatsContentType.BOOKS, RatsContentCategory.EBOOK)
    if category == "comic":
        return (RatsContentType.BOOKS, RatsContentCategory.COMICS)
    if category == "software":
        return (RatsContentType.SOFTWARE, RatsContentCategory.SOFTWARE)
    if category == "game":
        return (RatsContentType.GAMES, RatsContentCategory.GAME)
    return (RatsContentType.UNKNOWN, RatsContentCategory.UNKNOWN)


class RatsSearchConnector:
    """Minimal, high-efficiency connector for stock upstream `librats/rats-search`."""

    def __init__(
        self,
        classifier: Any,
        *,
        rest_base_url: str = "http://127.0.0.1:8095",
        manticore_host: str = "127.0.0.1",
        manticore_port: int = 9306,
        http_get_json: Optional[Callable[[str], Any]] = None,
        sql_executor: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        prune_malware_and_honeypots: bool = True,
        prune_dead_swarms: bool = False,
        prune_adult: bool = False,
        max_seen_cache: int = 50_000,
    ) -> None:
        self.classifier = classifier
        self.rest_base_url = rest_base_url.rstrip("/")
        self.manticore_host = manticore_host
        self.manticore_port = int(manticore_port)
        self._http_get_json = http_get_json or self._default_http_get_json
        self._sql_executor = sql_executor
        self.prune_malware_and_honeypots = prune_malware_and_honeypots
        self.prune_dead_swarms = prune_dead_swarms
        self.prune_adult = prune_adult
        self.max_seen_cache = max_seen_cache
        self._seen: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _default_http_get_json(url: str, timeout: float = 10.0) -> Any:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if isinstance(payload, Mapping) and "data" in payload:
            return payload["data"]
        return payload

    def default_mysql_cli_executor(self, sql: str) -> List[Dict[str, Any]]:
        """Execute a Manticore SQL query via local `mysql` client against `127.0.0.1:9306`."""
        cmd = [
            "mysql",
            "--protocol=TCP",
            f"-h{self.manticore_host}",
            f"-P{self.manticore_port}",
            "--batch",
            "--raw",
            "-e",
            sql,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5.0)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return []
        headers = lines[0].split("	")
        return [dict(zip(headers, ln.split("	"))) for ln in lines[1:]]

    def _mark_seen(self, infohash: str) -> None:
        if not infohash:
            return
        self._seen[infohash] = None
        self._seen.move_to_end(infohash)
        while len(self._seen) > self.max_seen_cache:
            self._seen.popitem(last=False)

    def _run_batch_inference(self, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        if hasattr(self.classifier, "classify_batch"):
            raw_preds = self.classifier.classify_batch(rows)
        elif hasattr(self.classifier, "predict_batch"):
            raw_preds = self.classifier.predict_batch(rows)
        elif hasattr(self.classifier, "classify"):
            raw_preds = [self.classifier.classify(r) for r in rows]
        else:
            raw_preds = [self.classifier.predict(r) for r in rows]
        return [normalize_prediction(p, r) for p, r in zip(raw_preds, rows)]

    @staticmethod
    def _rats_item_to_row(item: Mapping[str, Any]) -> Dict[str, Any]:
        infohash = str(item.get("hash") or item.get("infoHash") or item.get("infohash") or "").strip().lower()
        name = str(item.get("name") or item.get("title") or "")
        size = int(item.get("size") or 0)
        raw_files = item.get("fileList") or item.get("filesList") or item.get("files_list")
        if not isinstance(raw_files, list):
            raw_files = item.get("files") if isinstance(item.get("files"), list) else []
        files = [
            {"path": str(f.get("path") or f.get("name") or ""), "size": int(f.get("size") or 0)}
            if isinstance(f, Mapping)
            else {"path": str(f), "size": 0}
            for f in raw_files
        ]
        files_count_val = item.get("files")
        file_count = int(files_count_val) if isinstance(files_count_val, int) else (len(files) or 1)
        seeders = int(item.get("seeders") or 0)
        leechers = int(item.get("leechers") or 0)
        completed = item.get("completed")
        return {
            "infohash": infohash,
            "name": name,
            "size": size,
            "files": files,
            "file_count": max(1, file_count),
            "swarm": {
                "tracker_known": bool(seeders > 0 or leechers > 0),
                "tracker_seeders": seeders,
                "tracker_leechers": leechers,
                "tracker_completed": int(completed) if completed is not None else None,
            },
        }

    @staticmethod
    def build_remove_api_path(hashes: Sequence[str]) -> Optional[str]:
        """Build stock `rats-search` `/api/torrent.remove?hashes=[...]` relative URL."""
        clean = [h.strip().lower() for h in hashes if h and _HEX40_RE.match(h.strip())]
        if not clean:
            return None
        encoded = urllib.parse.quote(json.dumps(clean, separators=(",", ":")))
        return f"/api/torrent.remove?hashes={encoded}"

    @staticmethod
    def build_manticore_update_sql(infohash: str, content_type: int, content_category: int) -> Optional[Dict[str, Any]]:
        """Build an `O(1)` docid-indexed Manticore RT attribute update for `torrents`."""
        clean_hash = infohash.strip().lower()
        if not _HEX40_RE.match(clean_hash):
            return None
        row_id = row_id_from_hash(clean_hash)
        if row_id <= 0:
            return None
        t_id = int(content_type)
        c_id = int(content_category)
        return {
            "infoHash": clean_hash,
            "rowId": row_id,
            "contentType": t_id,
            "contentCategory": c_id,
            "verifySql": f"SELECT hash FROM torrents WHERE id = {row_id} LIMIT 1",
            "updateSql": f"UPDATE torrents SET contentType = {t_id}, contentCategory = {c_id} WHERE id = {row_id}",
        }

    def evaluate_batch(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Classify a batch of `rats-search` torrents and produce REST remove + Manticore update plans."""
        rows = [self._rats_item_to_row(it) for it in items]
        preds = self._run_batch_inference(rows)

        actions: List[Dict[str, Any]] = []
        delete_hashes: List[str] = []
        manticore_updates: List[Dict[str, Any]] = []

        for row, pred in zip(rows, preds):
            ih = pred["infohash"]
            is_unsafe = (not pred["csam_safe"]) or pred["disguised_executable"] or pred["junk"] == "junk"
            is_honeypot = bool(pred["honeypot_trap"])
            is_dead = pred["viability_tier"] == "dead"
            is_adult = pred["category"] == "xxx" or pred["adult"] == "adult"

            if self.prune_malware_and_honeypots and (is_unsafe or is_honeypot):
                reason = (
                    "csam_or_safety_gate"
                    if not pred["csam_safe"]
                    else "disguised_executable"
                    if pred["disguised_executable"]
                    else "honeypot_trap"
                    if is_honeypot
                    else "junk_or_sample"
                )
                if ih:
                    delete_hashes.append(ih)
                    self._mark_seen(ih)
                actions.append({
                    "hash": ih,
                    "action": "remove",
                    "reason": reason,
                    "armigerCategory": pred["category"],
                })
                continue

            if self.prune_adult and is_adult:
                if ih:
                    delete_hashes.append(ih)
                    self._mark_seen(ih)
                actions.append({
                    "hash": ih,
                    "action": "remove",
                    "reason": "adult_filtered",
                    "armigerCategory": pred["category"],
                })
                continue

            if self.prune_dead_swarms and is_dead:
                if ih:
                    delete_hashes.append(ih)
                    self._mark_seen(ih)
                actions.append({
                    "hash": ih,
                    "action": "remove",
                    "reason": "dead_swarm",
                    "armigerCategory": pred["category"],
                    "viabilityTier": pred["viability_tier"],
                })
                continue

            c_type, c_cat = map_to_rats_enums(pred, name=row["name"])
            sql_plan = self.build_manticore_update_sql(ih, int(c_type), int(c_cat))
            if sql_plan is not None:
                manticore_updates.append(sql_plan)
            if ih:
                self._mark_seen(ih)

            actions.append({
                "hash": ih,
                "rowId": row_id_from_hash(ih),
                "action": "update",
                "armigerCategory": pred["category"],
                "contentType": int(c_type),
                "contentTypeName": c_type.name.lower(),
                "contentCategory": int(c_cat),
                "contentCategoryName": c_cat.name.lower(),
                "lang": pred["lang"],
                "viabilityTier": pred["viability_tier"],
                "mondrianPassed": pred["mondrian_passed"],
            })

        return {
            "total": len(actions),
            "removedCount": len(delete_hashes),
            "deleteHashes": delete_hashes,
            "removeApiPath": self.build_remove_api_path(delete_hashes),
            "manticoreUpdates": manticore_updates,
            "actions": actions,
        }

    def apply_results(self, evaluation: Mapping[str, Any]) -> Dict[str, int]:
        """Apply REST `/api/torrent.remove` deletions and collision-checked Manticore `UPDATE`s."""
        removed = 0
        updated = 0
        collisions_skipped = 0

        remove_path = evaluation.get("removeApiPath")
        if remove_path:
            self._http_get_json(f"{self.rest_base_url}{remove_path}")
            removed = len(evaluation.get("deleteHashes") or [])

        executor = self._sql_executor
        if executor is not None:
            for plan in evaluation.get("manticoreUpdates") or []:
                rows = executor(plan["verifySql"])
                if not rows:
                    continue
                stored_hash = str(rows[0].get("hash") or "").strip().lower()
                if stored_hash != plan["infoHash"]:
                    collisions_skipped += 1
                    continue
                executor(plan["updateSql"])
                updated += 1

        return {
            "removed": removed,
            "updated": updated,
            "collisionsSkipped": collisions_skipped,
        }

    def sync_recent_once(self, limit: int = 100, enrich_files: bool = True, apply_changes: bool = True) -> Dict[str, Any]:
        """Poll `/api/search.recent`, fetch missing file lists via `/api/torrent.get`, classify, and apply."""
        recent_items = self._http_get_json(f"{self.rest_base_url}/api/search.recent?limit={int(limit)}")
        if not isinstance(recent_items, list):
            recent_items = []

        candidates: List[Dict[str, Any]] = []
        for item in recent_items:
            if not isinstance(item, Mapping):
                continue
            ih = str(item.get("hash") or "").strip().lower()
            if not _HEX40_RE.match(ih) or ih in self._seen:
                continue
            # Skip items that already have a populated rats-search ContentCategory (1..9 or 100)
            if int(item.get("contentCategory") or 0) != 0:
                self._mark_seen(ih)
                continue
            enriched = dict(item)
            if enrich_files and int(item.get("files") or 0) > 0 and not isinstance(item.get("fileList"), list):
                detail = self._http_get_json(f"{self.rest_base_url}/api/torrent.get?hash={ih}&files=true")
                if isinstance(detail, Mapping) and isinstance(detail.get("fileList"), list):
                    enriched["fileList"] = detail["fileList"]
            candidates.append(enriched)

        evaluation = self.evaluate_batch(candidates)
        if apply_changes:
            evaluation["applied"] = self.apply_results(evaluation)
        return evaluation

    def handle_websocket_event(self, message: str | Mapping[str, Any], apply_changes: bool = True) -> Optional[Dict[str, Any]]:
        """Process a `rats-search` WebSocket push message (`torrentIndexed` or `filesReady`)."""
        payload = json.loads(message) if isinstance(message, str) else message
        if not isinstance(payload, Mapping):
            return None
        event_name = str(payload.get("event") or "")
        data = payload.get("data")
        if event_name not in {"torrentIndexed", "filesReady"} or not isinstance(data, Mapping):
            return None

        ih = str(data.get("hash") or "").strip().lower()
        if not _HEX40_RE.match(ih):
            return None

        detail = self._http_get_json(f"{self.rest_base_url}/api/torrent.get?hash={ih}&files=true")
        item = detail if isinstance(detail, Mapping) and detail.get("hash") else data
        evaluation = self.evaluate_batch([item])
        if apply_changes:
            evaluation["applied"] = self.apply_results(evaluation)
        return evaluation


def create_server(connector: RatsSearchConnector, host: str = "127.0.0.1", port: int = 8097) -> ThreadingHTTPServer:
    """Create a lightweight HTTP server exposing `/healthz` and `/v1/rats-search/classify`."""

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok", "connector": "rats-search"})
            else:
                self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/v1/rats-search/classify":
                items = payload.get("torrents") or payload.get("items") or []
                result = connector.evaluate_batch(items)
                if payload.get("applyChanges"):
                    result["applied"] = connector.apply_results(result)
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "not_found"})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    model_dir = os.environ.get("ARMIGER_MODEL_DIR", ".")
    variant = os.environ.get("ARMIGER_VARIANT", "int8")
    rest_url = os.environ.get("RATS_REST_URL", "http://127.0.0.1:8095")
    manticore_host = os.environ.get("RATS_MANTICORE_HOST", "127.0.0.1")
    manticore_port = int(os.environ.get("RATS_MANTICORE_PORT", "9306"))
    enable_manticore_sql = os.environ.get("RATS_ENABLE_MANTICORE_SQL", "true").lower() == "true"
    poll_interval = float(os.environ.get("RATS_POLL_SECONDS", "5.0"))
    batch_size = int(os.environ.get("RATS_BATCH_SIZE", "100"))
    mode = os.environ.get("ARMIGER_CONNECTOR_MODE", "poll")

    clf = load_armiger_classifier(model_dir, variant=variant)
    connector = RatsSearchConnector(
        clf,
        rest_base_url=rest_url,
        manticore_host=manticore_host,
        manticore_port=manticore_port,
        prune_malware_and_honeypots=os.environ.get("ARMIGER_PRUNE_MALWARE", "true").lower() == "true",
        prune_dead_swarms=os.environ.get("ARMIGER_PRUNE_DEAD_SWARMS", "false").lower() == "true",
        prune_adult=os.environ.get("ARMIGER_PRUNE_ADULT", "false").lower() == "true",
    )
    if enable_manticore_sql:
        connector._sql_executor = connector.default_mysql_cli_executor

    if mode == "http":
        host = os.environ.get("ARMIGER_CONNECTOR_HOST", "127.0.0.1")
        port = int(os.environ.get("ARMIGER_CONNECTOR_PORT", "8097"))
        srv = create_server(connector, host=host, port=port)
        print(f"Armiger rats-search HTTP connector listening on {host}:{port}")
        srv.serve_forever()
    else:
        print(f"Armiger rats-search poller connected to {rest_url} (interval={poll_interval}s, batch={batch_size})")
        while True:
            try:
                connector.sync_recent_once(limit=batch_size, enrich_files=True, apply_changes=True)
            except Exception as exc:
                print(f"[rats-search-connector] poll error: {exc}", file=sys.stderr)
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
