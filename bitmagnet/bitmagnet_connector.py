#!/usr/bin/env python3
"""Armiger classification, GraphQL sync and Torznab filtering for stock Bitmagnet."""
from __future__ import annotations

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
ET.register_namespace("torznab", TORZNAB_NS)

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
_NON_KEBAB_RE = re.compile(r"[^a-z0-9]+")

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


def sanitize_bitmagnet_tag(tag: str) -> str:
    """Normalize to stock Bitmagnet's kebab-case and 30-character limit."""
    normalized = _NON_KEBAB_RE.sub("-", str(tag or "").strip().lower()).strip("-")
    if len(normalized) > 30:
        suffix = hashlib.sha256(normalized.encode()).hexdigest()[:8]
        normalized = normalized[:21].rstrip("-") + "-" + suffix
    return normalized


def _has_armiger_artifacts(mpath: Path) -> bool:
    has_ultra = (mpath / "armiger_ultra_pipeline.py").exists() and (
        (mpath / "armiger_ultra.fp32.onnx").exists() or (mpath / "armiger.int8.onnx").exists()
    )
    has_base = (mpath / "armiger_inference.py").exists() and (
        (mpath / "armiger.onnx").exists() or (mpath / "armiger.int8.onnx").exists()
    )
    return has_ultra or has_base


def ensure_armiger_model_dir(
    model_dir: str | Path,
    *,
    hf_repo: str = "NorviTech/armiger-ultra",
    auto_download: bool = False,
) -> Path:
    """Resolve `model_dir` across local/sibling/cache paths and optionally bootstrap from Hugging Face."""
    requested = Path(model_dir).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        requested,
        Path.cwd().resolve(),
        (repo_root.parent / "armiger").resolve(),
        (Path.home() / ".cache" / "armiger-ultra").resolve(),
    ]
    for cand in candidates:
        if _has_armiger_artifacts(cand):
            return cand

    if auto_download:
        target = requested
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = (Path.home() / ".cache" / "armiger-ultra").resolve()
            target.mkdir(parents=True, exist_ok=True)
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=hf_repo, local_dir=str(target))
        return target
    return requested


def load_armiger_classifier(
    model_dir: str | Path,
    variant: str = "ultra",
    *,
    preload_catalog: Optional[bool] = None,
    auto_download: bool = False,
    hf_repo: Optional[str] = None,
    opt_in_sharing: Optional[bool] = None,
) -> Any:
    """Load `ArmigerUltraPipeline` if present in `model_dir`, else `ArmigerONNXClassifier`."""
    # Installed Armiger owns code and verified data downloads. Never import code
    # from the model cache in this path; legacy HF bundles remain supported below.
    try:
        from armiger.artifacts import download_model
    except ImportError:
        download_model = None
    if download_model is not None:
        if hf_repo not in (None, "NorviTech/armiger-ultra"):
            raise ValueError("Use a local verified model directory with installed Armiger")
        from armiger import ArmigerUltraPipeline
        preset = "all" if variant in ("cpu", "int8", "armiger") else "ultra"
        mpath = download_model(model_dir, variant=preset, offline=not auto_download)
        if preload_catalog is None:
            preload_catalog = os.environ.get("ARMIGER_USE_ZERO_CUE_CATALOG", "true").lower() == "true"
        return ArmigerUltraPipeline.from_directory(
            mpath, variant=variant, preload_catalog=preload_catalog,
            rsi_mode=os.environ.get("ARMIGER_DHT_RSI_MODE") or None,
            opt_in_sharing=opt_in_sharing,
            offline=not auto_download,
        )
    eff_repo = hf_repo or os.environ.get("ARMIGER_HF_REPO", "NorviTech/armiger-ultra")
    mpath = ensure_armiger_model_dir(model_dir, hf_repo=eff_repo, auto_download=auto_download)
    if str(mpath) in sys.path:
        sys.path.remove(str(mpath))
    sys.path.insert(0, str(mpath))
    if preload_catalog is None:
        preload_catalog = os.environ.get("ARMIGER_USE_ZERO_CUE_CATALOG", "true").lower() == "true"
    if (mpath / "armiger_ultra_pipeline.py").exists():
        from armiger_ultra_pipeline import ArmigerUltraPipeline
        return ArmigerUltraPipeline.from_directory(
            mpath,
            variant=variant,
            preload_catalog=preload_catalog,
            rsi_mode=os.environ.get("ARMIGER_DHT_RSI_MODE") or None,
            opt_in_sharing=opt_in_sharing,
            offline=not auto_download,
        )
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
    get = pred.get if isinstance(pred, Mapping) else lambda key, default=None: getattr(pred, key, default)
    heads = get("heads", {})
    category = str(get("category") or _head_label(heads, "category", "unknown"))
    adult = str(get("adult") or _head_label(heads, "adult", "non_adult"))
    junk = str(get("junk") or _head_label(heads, "junk", "clean"))
    anime = str(get("anime") or _head_label(heads, "anime", "non_anime"))
    lang = str(get("lang") or _head_label(heads, "lang", "multi_or_unknown"))
    eng_subs = str(get("english_subs") or _head_label(heads, "english_subs", "unknown"))
    title_lang = str(get("title_language") or _head_label(heads, "title_language", "english_title"))
    owner_scope = str(get("owner_scope") or _head_label(heads, "owner_scope", "allowed_type"))
    gates = get("mondrian_gates", {})
    csam_safe = bool(get("csam_safety", {}).get("is_safe", True))
    honeypot = bool(get("honeypot_trap", {}).get("is_honeypot_trap", False))
    sw_obj = get("swarm_viability")
    sw_obj = sw_obj if isinstance(sw_obj, Mapping) else {}
    viability_tier = str(sw_obj.get("viability_tier", "unknown"))
    viability_prob = float(sw_obj.get("viability_prob", 0.5))
    survival_net_prob = float(sw_obj.get("survival_net_prob", viability_prob))

    disguised_exec = has_disguised_executable(
        str(row.get("name") or ""),
        row.get("files") or [],
        category,
    )
    span_bio = heads.get("span_bio") if isinstance(heads, Mapping) and isinstance(heads.get("span_bio"), Mapping) else None
    return {
        "infohash": str(row.get("infohash") or "").lower(),
        "category": category,
        "adult": adult,
        "junk": "junk" if disguised_exec else junk,
        "anime": anime,
        "anime_dub_status": (
            heads.get("anime", {}).get("anime_dub_status")
            if isinstance(heads, Mapping) and isinstance(heads.get("anime"), Mapping)
            else None
        ),
        "lang": lang,
        "english_subs": eng_subs,
        "title_language": title_lang,
        "owner_scope": owner_scope,
        "span_bio": span_bio,
        "csam_safe": csam_safe,
        "disguised_executable": disguised_exec,
        "honeypot_trap": honeypot,
        "viability_tier": viability_tier,
        "viability_prob": viability_prob,
        "survival_net_prob": survival_net_prob,
        "mondrian_passed": bool(gates.get("passed_all", True)) and not disguised_exec,
    }


def extract_torznab_item_row(item_elem: ET.Element) -> Dict[str, Any]:
    title = (item_elem.findtext("title") or "").strip()
    size_text = item_elem.findtext("size") or "0"
    try:
        size_val = int(size_text)
    except ValueError:
        size_val = 0

    attrs: Dict[str, str] = {}
    for attr in item_elem.findall(f"{{{TORZNAB_NS}}}attr"):
        name = (attr.attrib.get("name") or "").lower().strip()
        val = attr.attrib.get("value") or ""
        if name:
            attrs[name] = val

    seeders = int(attrs.get("seeders", "0") or 0)
    peers = int(attrs.get("peers", "0") or 0)
    leechers = max(peers - seeders, int(attrs.get("leechers", "0") or 0))
    grabs = int(attrs["grabs"]) if "grabs" in attrs and attrs["grabs"].strip() else None
    infohash = attrs.get("infohash") or ""

    files_attr = attrs.get("files") or ""
    # Torznab defines `files` as a file count, which carries no paths.
    if files_attr and not files_attr.strip().isdigit():
        files = [{"path": p.strip(), "size": size_val} for p in files_attr.split(";") if p.strip()]
    else:
        tail = title.rsplit(".", 1)[-1].lower() if "." in title else ""
        known_exts = {
            "mkv", "mp4", "avi", "flac", "mp3", "m4b", "epub", "pdf",
            "cbz", "cbr", "iso", "exe", "scr", "vbs", "bat", "cmd", "lnk", "ps1", "msi",
        }
        if tail in known_exts:
            files = [{"path": title, "size": size_val}]
        else:
            low = title.lower()
            if any(k in low for k in ("flac", "24bit", "lossless")):
                synth_ext = ".flac"
            elif any(k in low for k in ("m4b", "audiobook", "unabridged")):
                synth_ext = ".m4b"
            elif any(k in low for k in ("epub", "ebook")):
                synth_ext = ".epub"
            elif any(k in low for k in ("cbz", "cbr")):
                synth_ext = ".cbz"
            else:
                synth_ext = ".mkv"
            files = [{"path": f"{title}{synth_ext}", "size": size_val}]

    return {
        "infohash": infohash,
        "name": title,
        "size": size_val,
        "files": files,
        "file_count": len(files),
        "swarm": {
            "tracker_known": bool(seeders > 0 or leechers > 0 or "seeders" in attrs),
            "tracker_seeders": seeders,
            "tracker_leechers": leechers,
            "tracker_completed": grabs,
        },
    }


def filter_torznab_feed(
    xml_bytes: bytes,
    extract_row: Callable[[ET.Element], Dict[str, Any]],
    classify: Callable[[List[Dict[str, Any]]], Sequence[Any]],
    should_admit: Callable[[Any], Tuple[bool, str]],
    attributes: Callable[[Any], Sequence[Tuple[str, Any]]],
) -> Tuple[bytes, Dict[str, Any]]:
    """Filter a feed once, with connector-specific admission and annotation hooks."""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    items = [] if channel is None else channel.findall("item")
    stats = {"total": len(items), "admitted": 0, "blocked": 0, "blocked_reasons": {}}
    if not items:
        return xml_bytes, stats
    predictions = classify([extract_row(item) for item in items])
    for item, pred in zip(items, predictions):
        admit, reason = should_admit(pred)
        if not admit:
            channel.remove(item)
            stats["blocked"] += 1
            reasons = stats["blocked_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        stats["admitted"] += 1
        for name, value in attributes(pred):
            ET.SubElement(item, f"{{{TORZNAB_NS}}}attr", name=name, value=str(value))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), stats


class JSONHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class ClassifierConnector:
    """Shared inference, health reporting and bounded seen cache."""

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

    def health_payload(self) -> Dict[str, Any]:
        clf = self.classifier
        payload: Dict[str, Any] = {
            "status": "ok",
            "connector": self.connector_name,
            "pipeline": type(clf).__name__,
            "variant": str(getattr(clf, "variant", os.environ.get("ARMIGER_VARIANT", "ultra"))),
            "zeroCueCatalogLoaded": bool(getattr(clf, "_zero_cue_catalog", None) is not None),
            "swarmAdapterLoaded": bool(hasattr(getattr(clf, "swarm_adapter", None), "junk_encoder")),
        }
        if hasattr(clf, "health_status"):
            payload.update(clf.health_status())
        else:
            payload.update({
                "active_seq": int(getattr(clf, "active_seq", 0)),
                "active_bundle_merkle_root": getattr(clf, "active_bundle_merkle_root", None),
                "model_source": str(getattr(clf, "model_source", "baked_image")),
                "last_dht_check_ts": getattr(clf, "last_dht_check_ts", None),
                "sharing_status": clf.sharing_status() if hasattr(clf, "sharing_status") else None,
            })
        return payload


class BitmagnetConnector(ClassifierConnector):
    """Turnkey connector & Torznab pre-grab filter for stock upstream `bitmagnet-io/bitmagnet`."""

    connector_name = "bitmagnet"

    def __init__(
        self,
        classifier: Any,
        *,
        graphql_url: str = "http://localhost:3333/graphql",
        upstream_torznab_url: str = "http://localhost:3333/torznab",
        graphql_transport: Optional[Callable[[str, Mapping[str, Any]], Dict[str, Any]]] = None,
        prune_malware_and_honeypots: bool = True,
        prune_dead_swarms: bool = False,
        isolate_adult_namespace: bool = True,
        require_english_audio: bool = False,
        allow_english_subs: bool = True,
        reject_dead_swarms: bool = True,
        max_seen_cache: int = 50_000,
    ) -> None:
        self.classifier = classifier
        self.graphql_url = graphql_url
        self.upstream_torznab_url = upstream_torznab_url.rstrip("/")
        self._transport = graphql_transport or self._default_graphql_transport
        self.prune_malware_and_honeypots = prune_malware_and_honeypots
        self.prune_dead_swarms = prune_dead_swarms
        self.isolate_adult_namespace = isolate_adult_namespace
        self.require_english_audio = require_english_audio
        self.allow_english_subs = allow_english_subs
        self.reject_dead_swarms = reject_dead_swarms
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
            parsed = json.loads(resp.read().decode("utf-8"))
        if isinstance(parsed, Mapping) and parsed.get("errors"):
            raise RuntimeError(f"Bitmagnet GraphQL error: {parsed['errors']}")
        return parsed

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
        clean_tags = [
            sanitized
            for t in tag_names
            if t and (sanitized := sanitize_bitmagnet_tag(t))
        ]
        if not clean_hashes or not clean_tags:
            return None
        return {
            "query": GRAPHQL_PUT_TAGS_MUTATION,
            "variables": {
                "infoHashes": clean_hashes,
                "tagNames": list(dict.fromkeys(clean_tags)),
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
                    "disguised_executable"
                    if pred["disguised_executable"]
                    else "csam_or_safety_gate"
                    if not pred["csam_safe"]
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

            raw_tags = [
                f"armiger-{pred['category']}",
                f"armiger-lang-{pred['lang']}",
            ]
            if pred.get("title_language") == "non_english_title":
                raw_tags.append("armiger-title-non-english")
            elif pred.get("title_language") == "english_title":
                raw_tags.append("armiger-title-english")
            if pred["viability_tier"] != "unknown":
                raw_tags.append(f"armiger-viability-{pred['viability_tier']}")
            if pred["anime"] == "anime":
                raw_tags.append("armiger-anime")
                if pred.get("anime_dub_status"):
                    raw_tags.append(f"armiger-dub-{pred['anime_dub_status']}")
            if pred["english_subs"] == "has_eng_subs":
                raw_tags.append("armiger-eng-subs")

            tags = [sanitize_bitmagnet_tag(t) for t in raw_tags if sanitize_bitmagnet_tag(t)]
            tag_key = tuple(dict.fromkeys(tags))
            if ih:
                grouped_tags.setdefault(tag_key, []).append(ih)
                self._mark_seen(ih)

            action_entry: Dict[str, Any] = {
                "infoHash": ih,
                "action": "tag",
                "contentType": bitmagnet_type,
                "armigerCategory": pred["category"],
                "titleLanguage": pred.get("title_language", "english_title"),
                "ownerScope": pred.get("owner_scope", "allowed_type"),
                "tags": list(tag_key),
                "viabilityTier": pred["viability_tier"],
                "mondrianPassed": pred["mondrian_passed"],
            }
            if pred.get("span_bio"):
                action_entry["spanBio"] = {
                    "title": pred["span_bio"].get("title"),
                    "year": pred["span_bio"].get("year"),
                    "episode": pred["span_bio"].get("episode"),
                }
            actions.append(action_entry)

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
            if any(str(t).startswith(("armiger-", "armiger:")) for t in existing_tags):
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

    _extract_torznab_item_row = staticmethod(extract_torznab_item_row)

    def should_admit_torznab(self, pred: Mapping[str, Any]) -> Tuple[bool, str]:
        """Evaluate whether a Torznab XML item should be admitted to Prowlarr / `*Arr`."""
        if not pred["csam_safe"] or pred["disguised_executable"]:
            return False, "disguised_executable" if pred["disguised_executable"] else "csam_or_safety_blocked"
        if pred["honeypot_trap"]:
            return False, "honeypot_trap_blocked"
        if pred["junk"] == "junk" or not pred["mondrian_passed"]:
            return False, "mondrian_gate_blocked"
        if self.reject_dead_swarms and pred["viability_tier"] == "dead":
            return False, "dead_swarm_blocked"
        if self.require_english_audio and pred["lang"] == "non_english":
            has_subs = pred["english_subs"] == "has_eng_subs"
            if not (self.allow_english_subs and has_subs):
                return False, "non_english_audio_blocked"
        return True, "admitted"

    def filter_torznab_xml(self, xml_bytes: bytes) -> Tuple[bytes, Dict[str, Any]]:
        return filter_torznab_feed(
            xml_bytes, self._extract_torznab_item_row, self._run_batch_inference,
            self.should_admit_torznab,
            lambda pred: (
                ("armiger_category", pred["category"]),
                ("armiger_lang", pred["lang"]),
                ("armiger_title_lang", pred.get("title_language", "english_title")),
                ("armiger_viability", pred["viability_tier"]),
                ("armiger_survival_prob", f"{float(pred.get('survival_net_prob', pred['viability_prob'])):.4f}"),
            ),
        )

def create_server(connector: BitmagnetConnector, host: str = "127.0.0.1", port: int = 8096) -> ThreadingHTTPServer:
    """Create a lightweight HTTP server exposing `/healthz`, `/v1/bitmagnet/classify`, and `/torznab`."""

    class Handler(JSONHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, connector.health_payload())
                return

            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path.startswith("/torznab") or parsed.path.startswith("/api"):
                params = urllib.parse.parse_qs(parsed.query)
                t_param = (params.get("t") or [""])[0].lower()
                subpath = parsed.path[len("/torznab"):] if parsed.path.startswith("/torznab") else parsed.path
                target = f"{connector.upstream_torznab_url}{subpath}"
                if parsed.query:
                    target = f"{target}?{parsed.query}"
                try:
                    req = urllib.request.Request(target, method="GET")
                    with urllib.request.urlopen(req, timeout=15.0) as resp:
                        raw_xml = resp.read()
                        content_type = resp.headers.get("Content-Type", "application/rss+xml; charset=utf-8")
                except Exception as exc:
                    self._send_json(502, {"error": "upstream_torznab_error", "detail": str(exc)})
                    return

                if t_param in ("caps", ""):
                    out_bytes = raw_xml
                    stats = {"total": 0, "admitted": 0, "blocked": 0}
                else:
                    out_bytes, stats = connector.filter_torznab_xml(raw_xml)

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("X-Armiger-Total", str(stats["total"]))
                self.send_header("X-Armiger-Admitted", str(stats["admitted"]))
                self.send_header("X-Armiger-Blocked", str(stats["blocked"]))
                self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self.wfile.write(out_bytes)
                return

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

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    model_dir = os.environ.get("ARMIGER_MODEL_DIR", ".")
    variant = os.environ.get("ARMIGER_VARIANT", "ultra")
    auto_download = os.environ.get("ARMIGER_AUTO_DOWNLOAD", "true").lower() == "true"
    graphql_url = os.environ.get("BITMAGNET_GRAPHQL_URL", "http://localhost:3333/graphql")
    upstream_torznab_url = os.environ.get("UPSTREAM_TORZNAB_URL", "http://localhost:3333/torznab")
    poll_interval = float(os.environ.get("BITMAGNET_POLL_SECONDS", "5.0"))
    batch_size = int(os.environ.get("BITMAGNET_BATCH_SIZE", "100"))
    mode = os.environ.get("ARMIGER_CONNECTOR_MODE", "both").strip().lower()

    clf = load_armiger_classifier(model_dir, variant=variant, auto_download=auto_download)
    connector = BitmagnetConnector(
        clf,
        graphql_url=graphql_url,
        upstream_torznab_url=upstream_torznab_url,
        prune_malware_and_honeypots=os.environ.get("ARMIGER_PRUNE_MALWARE", "true").lower() == "true",
        prune_dead_swarms=os.environ.get("ARMIGER_PRUNE_DEAD_SWARMS", "false").lower() == "true",
        require_english_audio=os.environ.get("ARMIGER_REQUIRE_ENGLISH_AUDIO", "false").lower() == "true",
        allow_english_subs=os.environ.get("ARMIGER_ALLOW_ENGLISH_SUBS", "true").lower() == "true",
        reject_dead_swarms=os.environ.get("ARMIGER_REJECT_DEAD_SWARMS", "true").lower() == "true",
    )

    host = os.environ.get("ARMIGER_CONNECTOR_HOST") or os.environ.get("ARMIGER_BRIDGE_HOST") or "0.0.0.0"
    port = int(os.environ.get("ARMIGER_CONNECTOR_PORT") or os.environ.get("ARMIGER_BRIDGE_PORT") or "8096")
    proxy_port = int(os.environ.get("ARMIGER_PROXY_PORT", "8097"))
    enable_proxy_port = os.environ.get("ARMIGER_ENABLE_TORZNAB_PROXY", "true").lower() == "true"

    if mode == "http":
        if enable_proxy_port and proxy_port > 0 and proxy_port != port:
            proxy_srv = create_server(connector, host=host, port=proxy_port)
            threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()
            print(f"Armiger Torznab pre-grab proxy listening on {host}:{proxy_port}", flush=True)
        srv = create_server(connector, host=host, port=port)
        print(f"Armiger Bitmagnet HTTP connector listening on {host}:{port}", flush=True)
        srv.serve_forever()
        return

    if mode == "both":
        srv = create_server(connector, host=host, port=port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Armiger Bitmagnet HTTP connector listening on {host}:{port}", flush=True)
        if enable_proxy_port and proxy_port > 0 and proxy_port != port:
            proxy_srv = create_server(connector, host=host, port=proxy_port)
            threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()
            print(f"Armiger Torznab pre-grab proxy listening on {host}:{proxy_port}", flush=True)

    print(
        f"Armiger Bitmagnet poller connected to {graphql_url} "
        f"(variant={variant}, interval={poll_interval}s, batch={batch_size})",
        flush=True,
    )
    while True:
        try:
            connector.sync_once(limit=batch_size, apply_mutations=True)
        except Exception as exc:
            print(f"[bitmagnet-connector] poll error: {exc}", file=sys.stderr, flush=True)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
