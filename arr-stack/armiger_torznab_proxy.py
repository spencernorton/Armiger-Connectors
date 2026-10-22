"""Armiger Torznab Filtering Proxy & Pre-Grab Admission Gate for Prowlarr, Bitmagnet, and the *Arr Stack.

Sits transparently between upstream Torznab indexers (`Prowlarr`, `Bitmagnet`, `DH-Waypoint`, `Jackett`)
and downstream Torznab media managers (`Sonarr`, `Radarr`, `Lidarr`, `Readarr`).

Every Torznab XML `<item>` returned by an upstream search is evaluated in milliseconds by
`ArmigerUltraPipeline.classify_batch` before `Sonarr` or `Radarr` can grab it:
  1. Blocks disguised malware executables (`.mkv.exe`, `.scr`, `.vbs`, `.lnk`, `.ps1`, passworded archives).
  2. Blocks fake-seeder malware honeypots (`SwarmSurvivalNet` `is_honeypot_trap == True`).
  3. Blocks dead swarms (`viability_tier == "dead"` when `ARMIGER_REJECT_DEAD_SWARMS=true`).
  4. Blocks unrequested foreign-language audio traps (`AudioLanguageGuard` cross-head check when
     `ARMIGER_REQUIRE_ENGLISH_AUDIO=true`, with optional anime/subtitle allowance via `ARMIGER_ALLOW_ENGLISH_SUBS=true`).
  5. Enriches surviving `<item>` nodes with `<torznab:attr name="armiger_category" .../>` and
     `<torznab:attr name="armiger_viability" .../>` attributes.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
if str(_CONNECTOR_ROOT / "bitmagnet") not in sys.path:
    sys.path.insert(0, str(_CONNECTOR_ROOT / "bitmagnet"))

from bitmagnet_connector import ensure_armiger_model_dir  # noqa: E402

ROOT_DIR = ensure_armiger_model_dir(
    os.environ.get("ARMIGER_MODEL_DIR", str(_CONNECTOR_ROOT.parent / "armiger")),
    auto_download=os.environ.get("ARMIGER_AUTO_DOWNLOAD", "false").lower() == "true",
)
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from armiger_dht_rsi import DHTRSIMode  # noqa: E402
from armiger_ultra_pipeline import ArmigerUltraPipeline, ArmigerUltraPrediction  # noqa: E402

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
ET.register_namespace("torznab", TORZNAB_NS)


class ArmigerTorznabFilter:
    """Filters and enriches Torznab RSS/XML feeds using ArmigerUltraPipeline."""

    def __init__(
        self,
        pipeline: ArmigerUltraPipeline,
        *,
        require_english_audio: bool = False,
        allow_english_subs: bool = True,
        reject_dead_swarms: bool = True,
        allowed_categories: Optional[List[str]] = None,
        use_zero_cue_catalog: bool = True,
    ) -> None:
        self.pipeline = pipeline
        self.require_english_audio = require_english_audio
        self.allow_english_subs = allow_english_subs
        self.reject_dead_swarms = reject_dead_swarms
        self.allowed_categories = set(allowed_categories) if allowed_categories else None
        self.use_zero_cue_catalog = use_zero_cue_catalog

    @staticmethod
    def _extract_item_row(item_elem: ET.Element) -> Dict[str, Any]:
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
        if files_attr:
            files = [{"path": p.strip(), "size": size_val} for p in files_attr.split(";") if p.strip()]
        else:
            tail = title.rsplit(".", 1)[-1].lower() if "." in title else ""
            known_exts = {"mkv", "mp4", "avi", "flac", "mp3", "m4b", "epub", "pdf", "cbz", "cbr", "iso", "exe", "scr", "vbs", "bat", "cmd", "lnk", "ps1", "msi"}
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
            "swarm": {
                "tracker_known": bool(seeders > 0 or leechers > 0 or "seeders" in attrs),
                "tracker_seeders": seeders,
                "tracker_leechers": leechers,
                "tracker_completed": grabs,
            },
        }

    def should_admit(self, pred: ArmigerUltraPrediction) -> Tuple[bool, str]:
        """Evaluate whether a Torznab release should be admitted to Sonarr/Radarr/Lidarr/Readarr."""
        if not pred.csam_safety["is_safe"]:
            return False, str(pred.csam_safety.get("reason") or "csam_or_malware_blocked")
        if pred.honeypot_trap["is_honeypot_trap"]:
            return False, "honeypot_trap_blocked"
        if pred.junk == "junk" or not pred.mondrian_gates["passed_all"]:
            return False, str(pred.mondrian_gates.get("safety_gate_reason") or "mondrian_gate_blocked")

        if self.reject_dead_swarms and pred.swarm_viability["viability_tier"] == "dead":
            return False, "dead_swarm_blocked"

        if self.allowed_categories and pred.category not in self.allowed_categories:
            return False, f"category_mismatch:{pred.category}"

        if self.require_english_audio and pred.lang == "non_english":
            has_subs = pred.english_subs == "has_eng_subs"
            if not (self.allow_english_subs and has_subs):
                return False, "non_english_audio_blocked"

        return True, "admitted"

    def filter_torznab_xml(self, xml_bytes: bytes) -> Tuple[bytes, Dict[str, Any]]:
        """Parse upstream Torznab XML, remove blocked `<item>` nodes, and enrich surviving items."""
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return xml_bytes, {"total": 0, "admitted": 0, "blocked": 0, "blocked_reasons": {}}

        items = channel.findall("item")
        if not items:
            return xml_bytes, {"total": 0, "admitted": 0, "blocked": 0, "blocked_reasons": {}}

        rows = [self._extract_item_row(it) for it in items]
        predictions = self.pipeline.classify_batch(
            rows,
            use_zero_cue_catalog=self.use_zero_cue_catalog,
        )

        admitted_count = 0
        blocked_count = 0
        blocked_reasons: Dict[str, int] = {}

        for item_elem, pred in zip(items, predictions):
            admit, reason = self.should_admit(pred)
            if not admit:
                channel.remove(item_elem)
                blocked_count += 1
                blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
                continue

            admitted_count += 1
            for attr_name, attr_val in (
                ("armiger_category", pred.category),
                ("armiger_lang", pred.lang),
                ("armiger_viability", str(pred.swarm_viability["viability_tier"])),
                ("armiger_survival_prob", f"{pred.swarm_viability['survival_net_prob']:.4f}"),
            ):
                attr_el = ET.SubElement(item_elem, f"{{{TORZNAB_NS}}}attr")
                attr_el.set("name", attr_name)
                attr_el.set("value", attr_val)

        filtered_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        stats = {
            "total": len(items),
            "admitted": admitted_count,
            "blocked": blocked_count,
            "blocked_reasons": blocked_reasons,
        }
        return filtered_bytes, stats


def create_proxy_server(host: str = "localhost", port: int = 8097) -> ThreadingHTTPServer:
    model_dir = str(ensure_armiger_model_dir(os.environ.get("ARMIGER_MODEL_DIR", "/var/lib/armiger/model"), auto_download=os.environ.get("ARMIGER_AUTO_DOWNLOAD", "true").lower() == "true"))
    variant = os.environ.get("ARMIGER_VARIANT", "int8")
    upstream_url = os.environ.get("UPSTREAM_TORZNAB_URL", "http://dh-waypoint:3333/torznab").rstrip("/")
    pipeline = ArmigerUltraPipeline.from_directory(
        model_dir,
        variant=variant,
        rsi_mode=DHTRSIMode(os.environ.get("ARMIGER_DHT_RSI_MODE", "pull_only")),
        preload_catalog=os.environ.get("ARMIGER_USE_ZERO_CUE_CATALOG", "true").lower() == "true",
    )
    torznab_filter = ArmigerTorznabFilter(
        pipeline,
        require_english_audio=os.environ.get("ARMIGER_REQUIRE_ENGLISH_AUDIO", "false").lower() == "true",
        allow_english_subs=os.environ.get("ARMIGER_ALLOW_ENGLISH_SUBS", "true").lower() == "true",
        reject_dead_swarms=os.environ.get("ARMIGER_REJECT_DEAD_SWARMS", "true").lower() == "true",
    )

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                body = json.dumps({"status": "ok", "service": "armiger-torznab-proxy", "variant": variant}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            t_param = (params.get("t") or [""])[0].lower()
            target = f"{upstream_url}{parsed.path}"
            if parsed.query:
                target = f"{target}?{parsed.query}"

            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                raw_xml = resp.read()
                content_type = resp.headers.get("Content-Type", "application/rss+xml; charset=utf-8")

            if t_param in ("caps", ""):
                out_bytes = raw_xml
                stats = {"total": 0, "admitted": 0, "blocked": 0}
            else:
                out_bytes, stats = torznab_filter.filter_torznab_xml(raw_xml)

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Armiger-Total", str(stats["total"]))
            self.send_header("X-Armiger-Admitted", str(stats["admitted"]))
            self.send_header("X-Armiger-Blocked", str(stats["blocked"]))
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), ProxyHandler)


if __name__ == "__main__":
    listen_host = os.environ.get("ARMIGER_PROXY_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("ARMIGER_PROXY_PORT", "8097"))
    srv = create_proxy_server(listen_host, listen_port)
    print(f"Armiger Torznab proxy listening on {listen_host}:{listen_port}")
    srv.serve_forever()
