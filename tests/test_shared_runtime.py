"""Regression coverage for shared runtime and the standalone Arr entry points."""
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
import urllib.request

import pytest

import armiger_arr_webhook_receiver as webhook
import armiger_torznab_proxy as proxy
import bitmagnet_connector as bitmagnet
from rats_search_connector import RatsSearchConnector


@pytest.mark.parametrize("connector_type", [bitmagnet.BitmagnetConnector, RatsSearchConnector])
@pytest.mark.parametrize("api", ["classify_batch", "predict_batch", "classify", "predict"])
def test_shared_classifier_dispatch_cache_and_health(connector_type, api):
    result = {"category": "movie", "lang": "english"}
    classify = (lambda rows: [result for _ in rows]) if api.endswith("batch") else lambda row: result
    classifier = SimpleNamespace(**{api: classify}, active_seq=7)
    connector = connector_type(classifier, max_seen_cache=2)
    assert connector._run_batch_inference([]) == []
    assert connector._run_batch_inference([{"name": "Sintel.mkv"}])[0]["category"] == "movie"
    for key in ("a", "b", "a", "c"):
        connector._mark_seen(key)
    assert list(connector._seen) == ["a", "c"]
    assert connector.health_payload()["active_seq"] == 7
    assert connector.health_payload()["connector"] == connector.connector_name
    classifier.health_status = lambda: {"active_seq": 9, "sharing_status": {"enabled": False}}
    assert connector.health_payload()["sharing_status"] == {"enabled": False}
    assert connector.health_payload()["active_seq"] == 9


@pytest.mark.parametrize("layout", ["installed", "legacy", "base"])
@pytest.mark.parametrize("sharing", [None, False, True])
def test_shared_loader_preserves_model_layouts_and_sharing(monkeypatch, tmp_path, layout, sharing):
    pipeline = SimpleNamespace(from_directory=lambda directory, **kwargs: (directory, kwargs))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("ARMIGER_DHT_RSI_MODE", "pull_only")
    package = ModuleType("armiger")
    package.ArmigerUltraPipeline = pipeline
    monkeypatch.setitem(sys.modules, "armiger", package)
    if layout == "installed":
        artifacts = ModuleType("armiger.artifacts")
        artifacts.download_model = lambda directory, **kwargs: tmp_path
        monkeypatch.setitem(sys.modules, "armiger.artifacts", artifacts)
    else:
        monkeypatch.setitem(sys.modules, "armiger.artifacts", None)
        if layout == "legacy":
            legacy = ModuleType("armiger_ultra_pipeline")
            legacy.ArmigerUltraPipeline = pipeline
            monkeypatch.setitem(sys.modules, "armiger_ultra_pipeline", legacy)
            (tmp_path / "armiger_ultra_pipeline.py").touch()
            (tmp_path / "armiger.int8.onnx").touch()
        else:
            base = ModuleType("armiger_inference")
            base.ArmigerONNXClassifier = lambda **kwargs: (tmp_path, kwargs)
            monkeypatch.setitem(sys.modules, "armiger_inference", base)
            (tmp_path / "armiger_inference.py").touch()
            (tmp_path / "armiger.onnx").touch()
    before = list(sys.path)
    directory, kwargs = bitmagnet.load_armiger_classifier(tmp_path, variant="int8", opt_in_sharing=sharing)
    assert directory == tmp_path
    if layout == "base":
        assert kwargs["onnx_path"] == str(tmp_path / "armiger.onnx")
        return
    assert kwargs["opt_in_sharing"] is sharing
    assert kwargs["rsi_mode"] == "pull_only"
    assert kwargs["offline"] is True
    if layout == "installed":
        assert sys.path == before
    else:
        assert sys.path[0] == str(tmp_path)


def test_arr_imports_do_not_load_models(tmp_path):
    arr = Path(proxy.__file__).parent
    code = f"import sys; sys.path.insert(0, {str(arr)!r}); import armiger_torznab_proxy, armiger_arr_webhook_receiver"
    subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, check=True, timeout=10)
    assert not list(tmp_path.iterdir())


def prediction(**changes):
    fields = dict(category="movie", adult="non_adult", junk="clean", anime="non_anime",
                  lang="english", english_subs="unknown", mondrian_gates={"passed_all": True},
                  csam_safety={"is_safe": True}, honeypot_trap={"is_honeypot_trap": False},
                  swarm_viability={"viability_tier": "alive_viable", "survival_net_prob": 0.9})
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("changes,reason", [
    ({"junk": "junk"}, "mondrian_gate_blocked"),
    ({"csam_safety": {"is_safe": False}}, "csam_or_malware_blocked"),
    ({"honeypot_trap": {"is_honeypot_trap": True}}, "honeypot_trap_blocked"),
    ({"swarm_viability": {"viability_tier": "dead"}}, "dead_swarm_blocked"),
    ({"lang": "non_english"}, "non_english_audio_blocked"),
    ({"category": "music"}, "category_mismatch:music"),
])
def test_arr_admission_policies(changes, reason):
    gate = proxy.ArmigerTorznabFilter(None, require_english_audio=True, allowed_categories=["movie"])
    assert gate.should_admit(prediction(**changes)) == (False, reason)
    assert gate.should_admit(prediction(lang="non_english", english_subs="has_eng_subs")) == (True, "admitted")


def test_shared_xml_filter_preserves_arr_attributes_and_catalog_flag():
    seen = []
    def classify(rows, **kwargs):
        seen.append((rows, kwargs))
        return [prediction(), prediction(junk="junk")]
    gate = proxy.ArmigerTorznabFilter(SimpleNamespace(classify_batch=classify), use_zero_cue_catalog=False)
    feed = b'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item><title>Sintel.mkv</title><torznab:attr name="files" value="36"/></item><item><title>Sample.mkv</title></item></channel></rss>'
    output, stats = gate.filter_torznab_xml(feed)
    assert stats == {"total": 2, "admitted": 1, "blocked": 1, "blocked_reasons": {"mondrian_gate_blocked": 1}}
    assert seen[0][0][0]["files"] == [{"path": "Sintel.mkv", "size": 0}]
    assert seen[0][1] == {"use_zero_cue_catalog": False}
    assert b'armiger_survival_prob" value="0.9000"' in output
    assert b"Sample.mkv" not in output
    for empty in (b"<caps/>", b"<rss><channel/></rss>"):
        assert gate.filter_torznab_xml(empty) == (empty, {"total": 0, "admitted": 0, "blocked": 0, "blocked_reasons": {}})


@pytest.mark.parametrize("module,factory,service", [
    (proxy, proxy.create_proxy_server, "armiger-torznab-proxy"),
    (webhook, webhook.create_webhook_server, "armiger-arr-webhook-receiver"),
])
def test_arr_http_servers_use_shared_loader(monkeypatch, module, factory, service):
    calls = []
    pipeline = SimpleNamespace(opt_in_sharing=False)
    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return pipeline
    monkeypatch.setattr(module, "load_armiger_classifier", load)
    monkeypatch.setenv("ARMIGER_AUTO_DOWNLOAD", "false")
    server = factory("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as response:
            assert json.load(response)["service"] == service
        if module is webhook:
            for route in webhook.ROUTE_TO_ADAPTER:
                request = urllib.request.Request(base + route, data=b'{"eventType":"Test"}')
                with urllib.request.urlopen(request, timeout=3) as response:
                    assert json.load(response)["reason"] == "test_webhook_acknowledged"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert calls[0][1]["auto_download"] is False
    if module is webhook:
        assert calls[0][1]["preload_catalog"] is False


def test_webhook_feedback_preserves_ffprobe_and_explicit_sharing():
    calls = []
    pipeline = SimpleNamespace(opt_in_sharing=True, sharing_status=lambda: {},
                               ingest_rsi_feedback=lambda payload, **kwargs: (calls.append(kwargs) or (True, "verified", None)))
    service = webhook.ArmigerArrWebhookService(pipeline, enable_ffprobe=True,
                                              ffprobe_runner=lambda path: {"path": path}, opt_in_sharing=False)
    assert service.sharing_status()["opt_in_sharing_enabled"] is False
    result = service.process_webhook("/webhooks/radarr", {"movieFile": {"path": "/media/Sintel.mkv"}})
    assert result["verified"] is True
    assert calls == [{"adapter": "movie_webhook", "ffprobe_output": {"path": "/media/Sintel.mkv"}}]
