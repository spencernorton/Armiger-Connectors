"""Torznab admission proxy for Prowlarr and the *Arr stack."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

_CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
if str(_CONNECTOR_ROOT / "bitmagnet") not in sys.path:
    sys.path.insert(0, str(_CONNECTOR_ROOT / "bitmagnet"))

from bitmagnet_connector import (  # noqa: E402
    JSONHandler, extract_torznab_item_row, filter_torznab_feed, load_armiger_classifier,
)


class ArmigerTorznabFilter:
    """Filters and enriches Torznab RSS/XML feeds using Armiger predictions."""

    def __init__(
        self,
        pipeline: Any,
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

    _extract_item_row = staticmethod(extract_torznab_item_row)

    def should_admit(self, pred: Any) -> Tuple[bool, str]:
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
        return filter_torznab_feed(
            xml_bytes, self._extract_item_row,
            lambda rows: self.pipeline.classify_batch(rows, use_zero_cue_catalog=self.use_zero_cue_catalog),
            self.should_admit,
            lambda pred: (
                ("armiger_category", pred.category),
                ("armiger_lang", pred.lang),
                ("armiger_viability", pred.swarm_viability["viability_tier"]),
                ("armiger_survival_prob", f"{pred.swarm_viability['survival_net_prob']:.4f}"),
            ),
        )


def create_proxy_server(host: str = "localhost", port: int = 8097) -> ThreadingHTTPServer:
    variant = os.environ.get("ARMIGER_VARIANT", "int8")
    upstream_url = os.environ.get("UPSTREAM_TORZNAB_URL", "http://prowlarr:9696/1/api").rstrip("/")
    pipeline = load_armiger_classifier(
        os.environ.get("ARMIGER_MODEL_DIR", "/var/lib/armiger/model"), variant=variant,
        auto_download=os.environ.get("ARMIGER_AUTO_DOWNLOAD", "true").lower() == "true",
    )
    torznab_filter = ArmigerTorznabFilter(
        pipeline,
        require_english_audio=os.environ.get("ARMIGER_REQUIRE_ENGLISH_AUDIO", "false").lower() == "true",
        allow_english_subs=os.environ.get("ARMIGER_ALLOW_ENGLISH_SUBS", "true").lower() == "true",
        reject_dead_swarms=os.environ.get("ARMIGER_REJECT_DEAD_SWARMS", "true").lower() == "true",
    )

    class ProxyHandler(JSONHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok", "service": "armiger-torznab-proxy", "variant": variant})
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

    return ThreadingHTTPServer((host, port), ProxyHandler)


if __name__ == "__main__":
    listen_host = os.environ.get("ARMIGER_PROXY_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("ARMIGER_PROXY_PORT", "8097"))
    srv = create_proxy_server(listen_host, listen_port)
    print(f"Armiger Torznab proxy listening on {listen_host}:{listen_port}")
    srv.serve_forever()
