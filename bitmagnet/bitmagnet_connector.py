#!/usr/bin/env python3
"""Minimal, high-efficiency Armiger connector for stock upstream Bitmagnet.

Works with unmodified `bitmagnet-io/bitmagnet` via its standard GraphQL API
(`http://localhost:3333/graphql`):
  1. Queries newly indexed torrents (`torrentContent.search`) and skips items
     that already carry an `armiger:*` tag.
  2. Batch-fetches inner file lists in a single `torrent.files` GraphQL call.
  3. Runs a single vectorized Armiger batch inference pass (`ArmigerONNXClassifier`
     or `ArmigerUltraPipeline`).
  4. Prunes disguised executable lures (`.mkv.exe`, `.scr`, `.lnk`, `.vbs`),
     safety-gate failures, and fake-seeder honeypot traps in one batched
     `torrent.delete(infoHashes: $infoHashes)` mutation.
  5. Enriches surviving torrents by grouping hashes that share the same tag set
     into minimal batched `torrent.putTags(infoHashes: $infoHashes, tagNames: $tagNames)`
     mutations.
"""
from __future__ import annotations

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.request

BITMAGNET_CONTENT_TYPE_MAP: Dict[str, Optional[str]] = {
    "movie": "movie",
    "tv_show": "tv_show",
    "music": "music",
    "ebook": "ebook",
    "audiobook": "audiobook",
    "comic": "comic",
    "software": "software",
    "game": "game",
    "course": "ebook",
    "xxx": "xxx",
    "unknown": None,
}

_DISGUISED_EXEC_RE = re.compile(
    r"\.(?:mkv|mp4|avi|mov|wmv|flv|webm|mp3|flac|m4b|epub|pdf|cbz|cbr|srt|ass)"
    r"\.(?:exe|scr|bat|cmd|ps1|vbs|js|hta|msi|com|pif|lnk)$",
    re.IGNORECASE,
)
_STANDALONE_LURE_RE = re.compile(r"\.(?:scr|hta|pif|vbs|lnk)$", re.IGNORECASE)

GRAPHQL_RECENT_TORRENTS_QUERY = """
query ArmigerRecentTorrents($input: TorrentContentSearchQueryInput!) {
  torrentContent {
    search(input: $input) {
      items {
        infoHash
        contentType
        title
        seeders
        leechers
        torrent {
          infoHash
          name
          size
          filesCount
          hasFilesInfo
          seeders
          leechers
          tagNames
        }
      }
    }
  }
}
""".strip()

GRAPHQL_TORRENT_FILES_QUERY = """
query ArmigerTorrentFiles($input: TorrentFilesQueryInput!) {
  torrent {
    files(input: $input) {
      items {
        infoHash
        path
        size
      }
    }
  }
}
""".strip()

GRAPHQL_DELETE_MUTATION = """
mutation TorrentDelete($infoHashes: [Hash20!]!) {
  torrent {
    delete(infoHashes: $infoHashes)
  }
}
""".strip()

GRAPHQL_PUT_TAGS_MUTATION = """
mutation TorrentPutTags($infoHashes: [Hash20!]!, $tagNames: [String!]!) {
  torrent {
    putTags(infoHashes: $infoHashes, tagNames: $tagNames)
  }
}
""".strip()


def load_armiger_classifier(model_dir: str | Path, variant: str = "int8") -> Any:
    """Load `ArmigerUltraPipeline` if present in `model_dir`, else `ArmigerONNXClassifier`."""
    mpath = Path(model_dir).resolve()
    if str(mpath) in sys.path:
        sys.path.remove(str(mpath))
    sys.path.insert(0, str(mpath))
    if (mpath / "armiger_ultra_pipeline.py").exists():
        from armiger_ultra_pipeline import ArmigerUltraPipeline
        return ArmigerUltraPipeline.from_directory(mpath, variant=variant, preload_catalog=False)
    from armiger_inference import ArmigerONNXClassifier
    onnx_name = "armiger.onnx" if (mpath / "armiger.onnx").exists() else "armiger.int8.onnx"
    return ArmigerONNXClassifier(
        onnx_path=str(mpath / onnx_name),
        vocab_path=str(mpath / "vocab.json"),
        calibration_path=str(mpath / "calibration.json"),
    )


def has_disguised_executable(name: str, files: Sequence[Mapping[str, Any]], category: str) -> bool:
    """Deterministic safety check for double-extension lures (`.mkv.exe`, `.scr`, `.lnk`)."""
    candidates = [name] + [str(f.get("path") or f.get("name") or "") for f in files]
    for path in candidates:
        clean = path.strip()
        if not clean:
            continue
        if _DISGUISED_EXEC_RE.search(clean):
            return True
        if category not in {"software", "game"} and _STANDALONE_LURE_RE.search(clean):
            return True
    return False


def _head_label(heads: Any, key: str, default: str) -> str:
    if not isinstance(heads, Mapping):
        return default
    val = heads.get(key)
    if isinstance(val, Mapping):
        return str(val.get("label") or default)
    if isinstance(val, str) and val:
        return val
    return default


def normalize_prediction(pred: Any, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize either an `ArmigerUltraPrediction` object or an `ArmigerONNXClassifier` dict."""
    if isinstance(pred, Mapping):
        heads = pred.get("heads", {})
        category = str(pred.get("category") or _head_label(heads, "category", "unknown"))
        adult = str(pred.get("adult") or _head_label(heads, "adult", "non_adult"))
        junk = str(pred.get("junk") or _head_label(heads, "junk", "clean"))
        anime = str(pred.get("anime") or _head_label(heads, "anime", "non_anime"))
        lang = str(pred.get("lang") or _head_label(heads, "lang", "multi_or_unknown"))
        eng_subs = str(pred.get("english_subs") or _head_label(heads, "english_subs", "unknown"))
        gates = pred.get("mondrian_gates", {})
        csam_safe = bool(pred.get("csam_safety", {}).get("is_safe", True))
        honeypot = bool(pred.get("honeypot_trap", {}).get("is_honeypot_trap", False))
        viability_tier = str(pred.get("swarm_viability", {}).get("viability_tier", "unknown"))
        viability_prob = float(pred.get("swarm_viability", {}).get("viability_prob", 0.5))
    else:
        category = str(pred.category)
        heads = getattr(pred, "heads", {})
        adult = str(pred.adult)
        junk = str(pred.junk)
        anime = str(pred.anime)
        lang = str(pred.lang)
        eng_subs = str(pred.english_subs)
        gates = getattr(pred, "mondrian_gates", {})
        csam_safe = bool(getattr(pred, "csam_safety", {}).get("is_safe", True))
        honeypot = bool(getattr(pred, "honeypot_trap", {}).get("is_honeypot_trap", False))
        viability_tier = str(getattr(pred, "swarm_viability", {}).get("viability_tier", "unknown"))
        viability_prob = float(getattr(pred, "swarm_viability", {}).get("viability_prob", 0.5))

    disguised_exec = has_disguised_executable(
        str(row.get("name") or ""),
        row.get("files") or [],
        category,
    )
    return {
        "infohash": str(row.get("infohash") or "").lower(),
        "category": category,
        "adult": adult,
        "junk": "junk" if disguised_exec else junk,
        "anime": anime,
        "anime_dub_status": heads.get("anime", {}).get("anime_dub_status") if isinstance(heads, Mapping) and isinstance(heads.get("anime"), Mapping) else None,
        "lang": lang,
        "english_subs": eng_subs,
        "csam_safe": csam_safe,
        "disguised_executable": disguised_exec,
        "honeypot_trap": honeypot,
        "viability_tier": viability_tier,
        "viability_prob": viability_prob,
        "mondrian_passed": bool(gates.get("passed_all", True)) and not disguised_exec,
    }


class BitmagnetConnector:
    """Minimal, high-efficiency connector for stock upstream `bitmagnet-io/bitmagnet`."""

    def __init__(
        self,
        classifier: Any,
        *,
        graphql_url: str = "http://localhost:3333/graphql",
        graphql_transport: Optional[Callable[[str, Mapping[str, Any]], Dict[str, Any]]] = None,
        prune_malware_and_honeypots: bool = True,
        prune_dead_swarms: bool = False,
        isolate_adult_namespace: bool = True,
        max_seen_cache: int = 50_000,
    ) -> None:
        self.classifier = classifier
        self.graphql_url = graphql_url
        self._transport = graphql_transport or self._default_graphql_transport
        self.prune_malware_and_honeypots = prune_malware_and_honeypots
        self.prune_dead_swarms = prune_dead_swarms
        self.isolate_adult_namespace = isolate_adult_namespace
        self.max_seen_cache = max_seen_cache
        self._seen: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _default_graphql_transport(url: str, payload: Mapping[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

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
    def _item_to_row(item: Mapping[str, Any], files_by_hash: Optional[Mapping[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
        torrent_obj = item.get("torrent") if isinstance(item.get("torrent"), Mapping) else {}
        infohash = str(
            item.get("infoHash")
            or torrent_obj.get("infoHash")
            or item.get("info_hash")
            or item.get("infohash")
            or ""
        ).strip().lower()
        name = str(torrent_obj.get("name") or item.get("name") or item.get("title") or "")
        size = int(torrent_obj.get("size") or item.get("size") or 0)
        seeders = int(torrent_obj.get("seeders") or item.get("seeders") or 0)
        leechers = int(torrent_obj.get("leechers") or item.get("leechers") or 0)

        if files_by_hash and infohash in files_by_hash:
            files = files_by_hash[infohash]
        else:
            raw_files = item.get("files") if isinstance(item.get("files"), list) else []
            files = [
                {"path": str(f.get("path") or f.get("name") or ""), "size": int(f.get("size") or 0)}
                if isinstance(f, Mapping)
                else {"path": str(f), "size": 0}
                for f in raw_files
            ]

        file_count = int(torrent_obj.get("filesCount") or item.get("filesCount") or len(files) or 1)
        return {
            "infohash": infohash,
            "name": name,
            "size": size,
            "files": files,
            "file_count": file_count,
            "swarm": {
                "tracker_known": bool(seeders > 0 or leechers > 0),
                "tracker_seeders": seeders,
                "tracker_leechers": leechers,
                "tracker_completed": None,
            },
        }

    @staticmethod
    def build_delete_mutation(infohashes: Sequence[str]) -> Optional[Dict[str, Any]]:
        """Build stock Bitmagnet `torrent.delete(infoHashes: $infoHashes)` GraphQL payload."""
        clean = [h.strip().lower() for h in infohashes if h and h.strip()]
        if not clean:
            return None
        return {
            "query": GRAPHQL_DELETE_MUTATION,
            "variables": {"infoHashes": clean},
        }

    @staticmethod
    def build_put_tags_mutation(infohashes: Sequence[str], tag_names: Sequence[str]) -> Optional[Dict[str, Any]]:
        """Build stock Bitmagnet `torrent.putTags(infoHashes: $infoHashes, tagNames: $tagNames)` payload."""
        clean_hashes = [h.strip().lower() for h in infohashes if h and h.strip()]
        clean_tags = [t.strip() for t in tag_names if t and t.strip()]
        if not clean_hashes or not clean_tags:
            return None
        return {
            "query": GRAPHQL_PUT_TAGS_MUTATION,
            "variables": {
                "infoHashes": clean_hashes,
                "tagNames": clean_tags,
            },
        }

    def evaluate_batch(
        self,
        items: Sequence[Mapping[str, Any]],
        files_by_hash: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """Classify a batch of Bitmagnet items and group GraphQL delete/tag mutations."""
        rows = [self._item_to_row(it, files_by_hash) for it in items]
        preds = self._run_batch_inference(rows)

        actions: List[Dict[str, Any]] = []
        delete_hashes: List[str] = []
        grouped_tags: OrderedDict[Tuple[str, ...], List[str]] = OrderedDict()

        for pred in preds:
            ih = pred["infohash"]
            is_unsafe = (not pred["csam_safe"]) or pred["disguised_executable"] or pred["junk"] == "junk"
            is_honeypot = bool(pred["honeypot_trap"])
            is_dead = pred["viability_tier"] == "dead"

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
                    "infoHash": ih,
                    "action": "delete",
                    "reason": reason,
                    "category": pred["category"],
                })
                continue

            if self.prune_dead_swarms and is_dead:
                if ih:
                    delete_hashes.append(ih)
                    self._mark_seen(ih)
                actions.append({
                    "infoHash": ih,
                    "action": "delete",
                    "reason": "dead_swarm",
                    "category": pred["category"],
                    "viabilityTier": pred["viability_tier"],
                })
                continue

            if self.isolate_adult_namespace and pred["adult"] == "adult" and pred["category"] != "xxx":
                bitmagnet_type: Optional[str] = "xxx"
            else:
                bitmagnet_type = BITMAGNET_CONTENT_TYPE_MAP.get(pred["category"])

            tags = [
                f"armiger:{pred['category']}",
                f"armiger:lang:{pred['lang']}",
            ]
            if pred["viability_tier"] != "unknown":
                tags.append(f"armiger:viability:{pred['viability_tier']}")
            if pred["anime"] == "anime":
                tags.append("armiger:anime")
                if pred.get("anime_dub_status"):
                    tags.append(f"armiger:dub:{pred['anime_dub_status']}")
            if pred["english_subs"] == "has_eng_subs":
                tags.append("armiger:eng_subs")

            tag_key = tuple(dict.fromkeys(tags))
            if ih:
                grouped_tags.setdefault(tag_key, []).append(ih)
                self._mark_seen(ih)

            actions.append({
                "infoHash": ih,
                "action": "tag",
                "contentType": bitmagnet_type,
                "armigerCategory": pred["category"],
                "tags": list(tag_key),
                "viabilityTier": pred["viability_tier"],
                "mondrianPassed": pred["mondrian_passed"],
            })

        put_tags_mutations = [
            mut
            for tag_tuple, hashes in grouped_tags.items()
            if (mut := self.build_put_tags_mutation(hashes, list(tag_tuple))) is not None
        ]

        return {
            "total": len(actions),
            "deletedCount": len(delete_hashes),
            "deleteInfoHashes": delete_hashes,
            "actions": actions,
            "graphqlDeleteMutation": self.build_delete_mutation(delete_hashes),
            "graphqlPutTagsMutations": put_tags_mutations,
        }

    def sync_once(self, limit: int = 100, apply_mutations: bool = True) -> Dict[str, Any]:
        """Poll recent torrents from stock Bitmagnet GraphQL, classify untagged items, and apply mutations."""
        search_payload = {
            "query": GRAPHQL_RECENT_TORRENTS_QUERY,
            "variables": {
                "input": {
                    "limit": int(limit),
                    "orderBy": [{"field": "updated_at", "descending": True}],
                }
            },
        }
        resp = self._transport(self.graphql_url, search_payload)
        raw_items = (
            resp.get("data", {})
            .get("torrentContent", {})
            .get("search", {})
            .get("items", [])
        )

        untagged_items: List[Mapping[str, Any]] = []
        need_files_hashes: List[str] = []

        for item in raw_items:
            t_obj = item.get("torrent") if isinstance(item.get("torrent"), Mapping) else {}
            ih = str(item.get("infoHash") or t_obj.get("infoHash") or "").strip().lower()
            if not ih or ih in self._seen:
                continue
            existing_tags = t_obj.get("tagNames") or []
            if any(str(t).startswith("armiger:") for t in existing_tags):
                self._mark_seen(ih)
                continue
            untagged_items.append(item)
            if bool(t_obj.get("hasFilesInfo")) and int(t_obj.get("filesCount") or 0) > 0:
                need_files_hashes.append(ih)

        files_by_hash: Dict[str, List[Dict[str, Any]]] = {}
        if need_files_hashes:
            files_payload = {
                "query": GRAPHQL_TORRENT_FILES_QUERY,
                "variables": {
                    "input": {
                        "infoHashes": need_files_hashes,
                        "limit": min(1000, len(need_files_hashes) * 25),
                    }
                },
            }
            files_resp = self._transport(self.graphql_url, files_payload)
            file_items = (
                files_resp.get("data", {})
                .get("torrent", {})
                .get("files", {})
                .get("items", [])
            )
            for f_item in file_items:
                f_ih = str(f_item.get("infoHash") or "").strip().lower()
                if f_ih:
                    files_by_hash.setdefault(f_ih, []).append({
                        "path": str(f_item.get("path") or ""),
                        "size": int(f_item.get("size") or 0),
                    })

        result = self.evaluate_batch(untagged_items, files_by_hash=files_by_hash)
        if apply_mutations:
            if result["graphqlDeleteMutation"] is not None:
                self._transport(self.graphql_url, result["graphqlDeleteMutation"])
            for tag_mut in result["graphqlPutTagsMutations"]:
                self._transport(self.graphql_url, tag_mut)
        return result


def create_server(connector: BitmagnetConnector, host: str = "127.0.0.1", port: int = 8096) -> ThreadingHTTPServer:
    """Create a lightweight HTTP server exposing `/healthz` and `/v1/bitmagnet/classify`."""

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
                self._send_json(200, {"status": "ok", "connector": "bitmagnet"})
            else:
                self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/v1/bitmagnet/classify":
                items = payload.get("torrents") or payload.get("items") or []
                result = connector.evaluate_batch(items)
                if payload.get("applyMutations") and connector.graphql_url:
                    if result["graphqlDeleteMutation"] is not None:
                        connector._transport(connector.graphql_url, result["graphqlDeleteMutation"])
                    for tag_mut in result["graphqlPutTagsMutations"]:
                        connector._transport(connector.graphql_url, tag_mut)
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "not_found"})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    model_dir = os.environ.get("ARMIGER_MODEL_DIR", ".")
    variant = os.environ.get("ARMIGER_VARIANT", "int8")
    graphql_url = os.environ.get("BITMAGNET_GRAPHQL_URL", "http://localhost:3333/graphql")
    poll_interval = float(os.environ.get("BITMAGNET_POLL_SECONDS", "5.0"))
    batch_size = int(os.environ.get("BITMAGNET_BATCH_SIZE", "100"))
    mode = os.environ.get("ARMIGER_CONNECTOR_MODE", "poll")

    clf = load_armiger_classifier(model_dir, variant=variant)
    connector = BitmagnetConnector(
        clf,
        graphql_url=graphql_url,
        prune_malware_and_honeypots=os.environ.get("ARMIGER_PRUNE_MALWARE", "true").lower() == "true",
        prune_dead_swarms=os.environ.get("ARMIGER_PRUNE_DEAD_SWARMS", "false").lower() == "true",
    )

    if mode == "http":
        host = os.environ.get("ARMIGER_CONNECTOR_HOST", "127.0.0.1")
        port = int(os.environ.get("ARMIGER_CONNECTOR_PORT", "8096"))
        srv = create_server(connector, host=host, port=port)
        print(f"Armiger Bitmagnet HTTP connector listening on {host}:{port}")
        srv.serve_forever()
    else:
        print(f"Armiger Bitmagnet poller connected to {graphql_url} (interval={poll_interval}s, batch={batch_size})")
        while True:
            try:
                connector.sync_once(limit=batch_size, apply_mutations=True)
            except Exception as exc:
                print(f"[bitmagnet-connector] poll error: {exc}", file=sys.stderr)
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
