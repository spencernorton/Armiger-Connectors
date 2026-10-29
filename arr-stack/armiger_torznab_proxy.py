"""Torznab admission proxy for Prowlarr and the *Arr stack."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    JSONHandler,
    _head_label,
    extract_torznab_item_row,
    filter_torznab_feed,
    has_disguised_executable,
    load_armiger_classifier,
    parse_requested_torznab_categories,
)


def _pred_fields(pred: Any) -> Dict[str, Any]:
    get = pred.get if isinstance(pred, Mapping) else lambda key, default=None: getattr(pred, key, default)
    heads = get("heads") if isinstance(get("heads"), Mapping) else {}
    category = str(get("category") or _head_label(heads, "category", "unknown"))
    junk = str(get("junk") or _head_label(heads, "junk", "clean"))
    lang = str(get("lang") or _head_label(heads, "lang", "multi_or_unknown"))
    eng_subs = str(get("english_subs") or _head_label(heads, "english_subs", "unknown"))
    csam_raw = get("csam_safety")
    csam_dict = csam_raw if isinstance(csam_raw, Mapping) else {}
    csam_safe = bool(csam_dict.get("is_safe", get("csam_safe", True)))
    csam_reason = str(csam_dict.get("reason") or "csam_or_malware_blocked")
    hp_raw = get("honeypot_trap")
    honeypot = bool(hp_raw.get("is_honeypot_trap", False)) if isinstance(hp_raw, Mapping) else bool(hp_raw)
    gates_raw = get("mondrian_gates")
    gates_dict = gates_raw if isinstance(gates_raw, Mapping) else {}
    mondrian_passed = bool(gates_dict.get("passed_all", get("mondrian_passed", True)))
    mondrian_reason = str(gates_dict.get("safety_gate_reason") or "mondrian_gate_blocked")
    sw_raw = get("swarm_viability")
    sw_dict = sw_raw if isinstance(sw_raw, Mapping) else {}
    viability_tier = str(sw_dict.get("viability_tier") or get("viability_tier") or "unknown")
    viability_prob = float(sw_dict.get("viability_prob", get("viability_prob", 0.5)) or 0.5)
    survival_net_prob = float(sw_dict.get("survival_net_prob", get("survival_net_prob", viability_prob)) or viability_prob)
    disguised_exec = bool(get("disguised_executable", False))
    return {
        "category": category,
        "junk": junk,
        "lang": lang,
        "english_subs": eng_subs,
        "csam_safe": csam_safe,
        "csam_reason": csam_reason,
        "honeypot": honeypot,
        "mondrian_passed": mondrian_passed,
        "mondrian_reason": mondrian_reason,
        "viability_tier": viability_tier,
        "survival_net_prob": survival_net_prob,
        "disguised_executable": disguised_exec,
    }


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

    def _classify_rows(self, rows: List[Dict[str, Any]]) -> Sequence[Any]:
        if hasattr(self.pipeline, "classify_batch"):
            raw_preds = self.pipeline.classify_batch(rows, use_zero_cue_catalog=self.use_zero_cue_catalog)
        elif hasattr(self.pipeline, "predict_batch"):
            raw_preds = self.pipeline.predict_batch(rows)
        elif hasattr(self.pipeline, "classify"):
            raw_preds = [self.pipeline.classify(r) for r in rows]
        else:
            raw_preds = [self.pipeline.predict(r) for r in rows]
        checked: List[Any] = []
        for row, pred in zip(rows, raw_preds):
            fields = _pred_fields(pred)
            if has_disguised_executable(str(row.get("name") or ""), row.get("files") or [], fields["category"]):
                if isinstance(pred, Mapping):
                    flagged = dict(pred)
                    flagged["disguised_executable"] = True
                    checked.append(flagged)
                else:
                    try:
                        setattr(pred, "disguised_executable", True)
                        checked.append(pred)
                    except Exception:
                        flagged = dict(fields)
                        flagged["disguised_executable"] = True
                        checked.append(flagged)
            else:
                checked.append(pred)
        return checked

    def should_admit(self, pred: Any) -> Tuple[bool, str]:
        """Evaluate whether a Torznab release should be admitted to Sonarr/Radarr/Lidarr/Readarr."""
        f = _pred_fields(pred)
        if f["disguised_executable"]:
            return False, "disguised_executable"
        if not f["csam_safe"]:
            return False, f["csam_reason"]
        if f["honeypot"]:
            return False, "honeypot_trap_blocked"
        if f["junk"] == "junk" or not f["mondrian_passed"]:
            return False, f["mondrian_reason"]

        if self.reject_dead_swarms and f["viability_tier"] in ("dead", "dead_known_zero"):
            return False, "dead_swarm_blocked"

        if self.allowed_categories and f["category"] not in self.allowed_categories:
            return False, f"category_mismatch:{f['category']}"

        if self.require_english_audio and f["lang"] == "non_english":
            has_subs = f["english_subs"] == "has_eng_subs"
            if not (self.allow_english_subs and has_subs):
                return False, "non_english_audio_blocked"

        return True, "admitted"

    def filter_torznab_xml(
        self,
        xml_bytes: bytes,
        *,
        allowed_category_ids: Optional[set[int]] = None,
    ) -> Tuple[bytes, Dict[str, Any]]:
        return filter_torznab_feed(
            xml_bytes,
            self._extract_item_row,
            self._classify_rows,
            self.should_admit,
            lambda pred: (
                ("armiger_category", _pred_fields(pred)["category"]),
                ("armiger_lang", _pred_fields(pred)["lang"]),
                ("armiger_viability", _pred_fields(pred)["viability_tier"]),
                ("armiger_survival_prob", f"{_pred_fields(pred)['survival_net_prob']:.4f}"),
            ),
            allowed_category_ids=allowed_category_ids,
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
                allowed_cat_ids = parse_requested_torznab_categories(t_param, params.get("cat"))
                out_bytes, stats = torznab_filter.filter_torznab_xml(
                    raw_xml,
                    allowed_category_ids=allowed_cat_ids,
                )

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
