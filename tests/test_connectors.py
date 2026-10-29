"""Unit and HTTP/wire-protocol integration tests for `Armiger-Connectors` (`bitmagnet` & `rats-search`)."""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, List, Mapping, Sequence
import urllib.request
import xml.etree.ElementTree as ET

from bitmagnet_connector import (
    BITMAGNET_VIABILITY_TAG_MAP,
    LEGACY_VIABILITY_TAG_REPAIR_MAP,
    BitmagnetConnector,
    create_server as create_bitmagnet_server,
    has_disguised_executable,
    sanitize_bitmagnet_tag,
)
from rats_search_connector import (
    RatsContentCategory,
    RatsContentType,
    RatsSearchConnector,
    create_server as create_rats_server,
    execute_manticore_mysql41,
    map_to_rats_enums,
    row_id_from_hash,
)


class SyntheticArmigerClassifier:
    """Deterministic stub classifier matching `ArmigerONNXClassifier` / `ArmigerUltraPipeline`."""

    def classify_batch(self, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows:
            name = str(row.get("name") or "").lower()
            seeders = int(row.get("swarm", {}).get("tracker_seeders") or 0)
            leechers = int(row.get("swarm", {}).get("tracker_leechers") or 0)

            category = "movie"
            anime = "non_anime"
            adult = "non_adult"
            junk = "clean"
            lang = "english"
            eng_subs = "unknown"
            honeypot = False
            viability_tier = "alive_viable" if seeders > 0 else "dead"

            if "s01e" in name or "season" in name:
                category = "tv_show"
            elif "goldberg" in name or "flac" in name:
                category = "music"
            elif "gutenberg" in name or "epub" in name:
                category = "ebook"
            elif "pepper" in name or "cbz" in name:
                category = "comic"
            elif "ubuntu" in name or "iso" in name:
                category = "software"
            elif "freedoom" in name or "linux.tar" in name:
                category = "game"
            elif "adult_test" in name or "xxx" in name:
                category = "xxx"
                adult = "adult"

            if "anime" in name:
                anime = "anime"
                eng_subs = "has_eng_subs"
            if "sample" in name:
                junk = "junk"
            if seeders >= 5000 and leechers == 0 and "trap" in name:
                honeypot = True

            out.append({
                "category": category,
                "adult": adult,
                "junk": junk,
                "anime": anime,
                "lang": lang,
                "english_subs": eng_subs,
                "heads": {"anime": {"label": anime, "anime_dub_status": "subbed" if anime == "anime" else None}},
                "mondrian_gates": {"passed_all": junk == "clean" and adult == "non_adult"},
                "csam_safety": {"is_safe": True},
                "honeypot_trap": {"is_honeypot_trap": honeypot},
                "swarm_viability": {"viability_tier": viability_tier, "viability_prob": 0.91 if seeders > 0 else 0.04},
            })
        return out


# =====================================================================
# 1. Stock Upstream Bitmagnet Connector Tests
# =====================================================================

def test_disguised_executable_and_kebab_tag_sanitization() -> None:
    assert has_disguised_executable(
        "Sintel.2010.1080p.BluRay.x264",
        [{"path": "Sintel.2010.1080p.mkv.exe", "size": 2_400_000}],
        "movie",
    ) is True
    assert has_disguised_executable(
        "Tears.of.Steel.2012.1080p",
        [{"path": "codec_pack.scr", "size": 900_000}],
        "movie",
    ) is True
    assert has_disguised_executable(
        "Sintel.2010.1080p.BluRay.x264",
        [{"path": "Sintel.2010.1080p.mkv", "size": 4_500_000_000}],
        "movie",
    ) is False

    # Stock Bitmagnet enforces `^[a-z0-9]+(-[a-z0-9]+)*$` on `torrent_tags.name`
    assert sanitize_bitmagnet_tag("armiger:tv_show") == "armiger-tv-show"
    assert sanitize_bitmagnet_tag("armiger:lang:multi_or_unknown") == "armiger-lang-multi-or-unknown"
    assert sanitize_bitmagnet_tag("armiger-viability-alive_viable") == "armiger-viability-alive-viable"


def test_bitmagnet_accepts_long_viability_labels_without_merging_distinct_tags() -> None:
    labels = ("armiger-viability-dead-known-zero", "armiger-viability-dead-known-unknown")
    tags = [sanitize_bitmagnet_tag(label) for label in labels]
    assert len(set(tags)) == 2
    for label, tag in zip(labels, tags):
        assert len(tag) <= 30
        assert tag.startswith("armiger-") and not tag.endswith("-")
        assert sanitize_bitmagnet_tag(label) == tag
        assert sanitize_bitmagnet_tag(tag) == tag
    mutation = BitmagnetConnector.build_put_tags_mutation(["ab" * 20], labels)
    assert mutation["variables"]["tagNames"] == tags


def test_canonical_viability_tag_mapping_and_legacy_aliases() -> None:
    expected = {
        "alive_viable": "armiger-viability-alive-viable",
        "degraded_viable": "armiger-viability-degraded",
        "suspect_stalled": "armiger-viability-stalled",
        "dead_known_zero": "armiger-viability-dead",
        "dead": "armiger-viability-dead",
        "unknown_cold": "armiger-viability-unknown-cold",
    }
    assert BITMAGNET_VIABILITY_TAG_MAP == expected
    for tier, canonical_tag in expected.items():
        assert len(canonical_tag) <= 30
        assert sanitize_bitmagnet_tag(f"armiger-viability-{tier}") == canonical_tag
        assert sanitize_bitmagnet_tag(canonical_tag) == canonical_tag

    # Legacy hash-truncated tags normalize to canonical Option 2 tags on putTags
    assert sanitize_bitmagnet_tag("armiger-viability-dea-c24281f7") == "armiger-viability-dead"
    assert sanitize_bitmagnet_tag("armiger-viability-deg-7c170f8b") == "armiger-viability-degraded"
    assert sanitize_bitmagnet_tag("armiger-viability-sus-cdc9d2c6") == "armiger-viability-stalled"

    # build_delete_tags_mutation must preserve exact legacy tag names so Bitmagnet deletes them
    del_mut = BitmagnetConnector.build_delete_tags_mutation(
        ["ab" * 20],
        list(LEGACY_VIABILITY_TAG_REPAIR_MAP.keys()),
    )
    assert del_mut is not None
    assert "deleteTags(infoHashes: $infoHashes, tagNames: $tagNames)" in del_mut["query"]
    assert del_mut["variables"]["tagNames"] == [
        "armiger-viability-dea-c24281f7",
        "armiger-viability-deg-7c170f8b",
        "armiger-viability-sus-cdc9d2c6",
    ]


def test_bitmagnet_repair_legacy_viability_tags_and_opportunistic_sync_repair() -> None:
    rows: List[Dict[str, Any]] = [
        {
            "infoHash": f"{1:040x}",
            "title": "Dead Swarm A",
            "torrent": {
                "infoHash": f"{1:040x}",
                "name": "Dead.Swarm.A.1080p",
                "size": 1_000_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 0,
                "leechers": 1,
                "tagNames": ["armiger-movie", "armiger-lang-english", "armiger-viability-dea-c24281f7"],
            },
        },
        {
            "infoHash": f"{2:040x}",
            "title": "Dead Swarm B",
            "torrent": {
                "infoHash": f"{2:040x}",
                "name": "Dead.Swarm.B.1080p",
                "size": 1_000_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 0,
                "leechers": 2,
                "tagNames": ["armiger-movie", "armiger-lang-english", "armiger-viability-dea-c24281f7"],
            },
        },
        {
            "infoHash": f"{3:040x}",
            "title": "Degraded Swarm C",
            "torrent": {
                "infoHash": f"{3:040x}",
                "name": "Degraded.Swarm.C.1080p",
                "size": 1_500_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 1,
                "leechers": 5,
                "tagNames": ["armiger-tv-show", "armiger-lang-english", "armiger-viability-deg-7c170f8b"],
            },
        },
        {
            "infoHash": f"{4:040x}",
            "title": "Stalled Swarm D",
            "torrent": {
                "infoHash": f"{4:040x}",
                "name": "Stalled.Swarm.D.1080p",
                "size": 2_000_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 0,
                "leechers": 8,
                "tagNames": ["armiger-software", "armiger-lang-english", "armiger-viability-sus-cdc9d2c6"],
            },
        },
    ]
    mutation_order: List[str] = []

    def fake_transport(url: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        q = str(payload.get("query") or "")
        variables = payload.get("variables") or {}
        if "ArmigerRecentTorrents" in q:
            inp = variables.get("input") or {}
            lim = int(inp.get("limit") or 100)
            off = int(inp.get("offset") or 0)
            facet_filter = (
                ((inp.get("facets") or {}).get("torrentTag") or {}).get("filter") or []
            )
            if facet_filter:
                matched = [
                    r for r in rows
                    if any(t in (r["torrent"].get("tagNames") or []) for t in facet_filter)
                ]
            else:
                matched = list(rows)
            page = matched[off : off + lim]
            return {
                "data": {
                    "torrentContent": {
                        "search": {
                            "hasNextPage": (off + lim) < len(matched),
                            "items": page,
                        }
                    }
                }
            }
        if "TorrentPutTags" in q:
            mutation_order.append("putTags")
            tag_hashes = set(variables.get("infoHashes") or [])
            new_tags = list(variables.get("tagNames") or [])
            for r in rows:
                if r["infoHash"] in tag_hashes:
                    existing = list(r["torrent"].get("tagNames") or [])
                    r["torrent"]["tagNames"] = list(dict.fromkeys(existing + new_tags))
            return {"data": {"torrent": {"putTags": None}}}
        if "TorrentDeleteTags" in q:
            mutation_order.append("deleteTags")
            tag_hashes = set(variables.get("infoHashes") or [])
            del_tags = set(variables.get("tagNames") or [])
            for r in rows:
                if r["infoHash"] in tag_hashes:
                    existing = list(r["torrent"].get("tagNames") or [])
                    r["torrent"]["tagNames"] = [t for t in existing if t not in del_tags]
            return {"data": {"torrent": {"deleteTags": None}}}
        return {"data": {}}

    connector = BitmagnetConnector(SyntheticArmigerClassifier(), graphql_transport=fake_transport)
    report = connector.repair_legacy_viability_tags(limit=1, max_pages=10, apply_mutations=True)
    assert report["repairedCount"] == 4
    assert report["complete"] is True
    assert report["byTag"] == {
        "armiger-viability-dea-c24281f7": 2,
        "armiger-viability-deg-7c170f8b": 1,
        "armiger-viability-sus-cdc9d2c6": 1,
    }
    assert "armiger-viability-dead" in rows[0]["torrent"]["tagNames"]
    assert "armiger-viability-dea-c24281f7" not in rows[0]["torrent"]["tagNames"]
    assert "armiger-movie" in rows[0]["torrent"]["tagNames"]
    assert "armiger-viability-dead" in rows[1]["torrent"]["tagNames"]
    assert "armiger-viability-dea-c24281f7" not in rows[1]["torrent"]["tagNames"]
    assert "armiger-viability-degraded" in rows[2]["torrent"]["tagNames"]
    assert "armiger-viability-deg-7c170f8b" not in rows[2]["torrent"]["tagNames"]
    assert "armiger-viability-stalled" in rows[3]["torrent"]["tagNames"]
    assert "armiger-viability-sus-cdc9d2c6" not in rows[3]["torrent"]["tagNames"]
    assert mutation_order[0] == "putTags"

    # Second pass is a no-op once legacy tags are removed
    second = connector.repair_legacy_viability_tags(limit=10, max_pages=10, apply_mutations=True)
    assert second["repairedCount"] == 0
    assert second["graphqlPutTagsMutations"] == []
    assert second["graphqlDeleteTagsMutations"] == []

    # Opportunistic sync_once repair if a scanned page contains a legacy tag
    rows[0]["torrent"]["tagNames"].append("armiger-viability-dea-c24281f7")
    rows[0]["torrent"]["tagNames"].remove("armiger-viability-dead")
    sync_res = connector.sync_once(limit=10, apply_mutations=True)
    assert sync_res.get("repairedLegacyCount") == 1
    assert "armiger-viability-dead" in rows[0]["torrent"]["tagNames"]
    assert "armiger-viability-dea-c24281f7" not in rows[0]["torrent"]["tagNames"]


def test_bitmagnet_evaluate_batch_groups_tags_and_prunes_malware() -> None:
    connector = BitmagnetConnector(SyntheticArmigerClassifier())
    items = [
        {
            "infoHash": "1111111111111111111111111111111111111111",
            "name": "Sintel.2010.1080p.WEBM-OPEN",
            "size": 4_200_000_000,
            "seeders": 45,
            "leechers": 4,
            "files": [{"path": "Sintel.2010.1080p.mkv", "size": 4_200_000_000}],
        },
        {
            "infoHash": "2222222222222222222222222222222222222222",
            "name": "Tears.of.Steel.2012.1080p.WEBM-OPEN",
            "size": 3_800_000_000,
            "seeders": 30,
            "leechers": 2,
            "files": [{"path": "Tears.of.Steel.2012.1080p.mkv", "size": 3_800_000_000}],
        },
        {
            "infoHash": "3333333333333333333333333333333333333333",
            "name": "Big.Buck.Bunny.2008.1080p.Lure",
            "size": 15_000_000,
            "seeders": 120,
            "leechers": 0,
            "files": [{"path": "Big.Buck.Bunny.2008.1080p.mkv.exe", "size": 15_000_000}],
        },
    ]

    res = connector.evaluate_batch(items)
    assert res["total"] == 3
    assert res["deletedCount"] == 1
    assert res["deleteInfoHashes"] == ["3333333333333333333333333333333333333333"]

    del_mut = res["graphqlDeleteMutation"]
    assert del_mut is not None
    assert "torrent {" in del_mut["query"]
    assert "delete(infoHashes: $infoHashes)" in del_mut["query"]
    assert del_mut["variables"] == {"infoHashes": ["3333333333333333333333333333333333333333"]}

    # Both surviving movies share the exact same kebab-case tag set, so they are grouped into 1 putTags mutation
    assert len(res["graphqlPutTagsMutations"]) == 1
    tag_mut = res["graphqlPutTagsMutations"][0]
    assert "putTags(infoHashes: $infoHashes, tagNames: $tagNames)" in tag_mut["query"]
    assert tag_mut["variables"]["infoHashes"] == [
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
    ]
    assert "armiger-movie" in tag_mut["variables"]["tagNames"]
    assert "armiger-lang-english" in tag_mut["variables"]["tagNames"]
    assert "armiger-viability-alive-viable" in tag_mut["variables"]["tagNames"]


def test_bitmagnet_sync_once_fetches_files_and_skips_already_tagged() -> None:
    calls: List[Mapping[str, Any]] = []

    def fake_transport(url: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        calls.append(payload)
        q = str(payload.get("query") or "")
        if "ArmigerRecentTorrents" in q:
            return {
                "data": {
                    "torrentContent": {
                        "search": {
                            "items": [
                                {
                                    "infoHash": "aaaa111111111111111111111111111111111111",
                                    "contentType": None,
                                    "title": "Already Tagged Movie",
                                    "seeders": 10,
                                    "leechers": 1,
                                    "torrent": {
                                        "infoHash": "aaaa111111111111111111111111111111111111",
                                        "name": "Sintel.2010.1080p",
                                        "size": 4_000_000_000,
                                        "filesCount": 1,
                                        "hasFilesInfo": True,
                                        "seeders": 10,
                                        "leechers": 1,
                                        "tagNames": ["armiger-movie"],
                                    },
                                },
                                {
                                    "infoHash": "bbbb222222222222222222222222222222222222",
                                    "contentType": None,
                                    "title": "Cosmos Laundromat",
                                    "seeders": 18,
                                    "leechers": 3,
                                    "torrent": {
                                        "infoHash": "bbbb222222222222222222222222222222222222",
                                        "name": "Cosmos.Laundromat.2015.1080p",
                                        "size": 2_100_000_000,
                                        "filesCount": 1,
                                        "hasFilesInfo": True,
                                        "seeders": 18,
                                        "leechers": 3,
                                        "tagNames": [],
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        if "ArmigerTorrentFiles" in q:
            return {
                "data": {
                    "torrent": {
                        "files": {
                            "items": [
                                {
                                    "infoHash": "bbbb222222222222222222222222222222222222",
                                    "path": "Cosmos.Laundromat.2015.1080p.mkv",
                                    "size": 2_100_000_000,
                                }
                            ]
                        }
                    }
                }
            }
        return {"data": {}}

    connector = BitmagnetConnector(SyntheticArmigerClassifier(), graphql_transport=fake_transport)
    result = connector.sync_once(limit=50, apply_mutations=True)
    assert result["total"] == 1
    assert result["actions"][0]["infoHash"] == "bbbb222222222222222222222222222222222222"
    # 1 search query + 1 files query + 1 putTags mutation = 3 GraphQL calls
    assert len(calls) == 3


def test_bitmagnet_sync_once_paginates_across_minute_flush_and_handles_deletes() -> None:
    # Simulate stock Bitmagnet's 60s flush: 350 newly saved torrents (including 2 malware lures
    # in the first page) sitting above 50 historical already-tagged torrents in `updated_at DESC`.
    rows: List[Dict[str, Any]] = []
    for idx in range(350):
        ih = f"{idx + 1:040x}"
        is_lure = idx in (10, 25)
        name = f"Fake.Lure.{idx}.2025.1080p" if is_lure else f"Sintel.Part.{idx}.2010.1080p"
        rows.append({
            "infoHash": ih,
            "contentType": None,
            "title": name,
            "seeders": 20,
            "leechers": 2,
            "torrent": {
                "infoHash": ih,
                "name": name,
                "size": 15_000_000 if is_lure else 2_000_000_000,
                "filesCount": 1,
                "hasFilesInfo": True,
                "seeders": 20,
                "leechers": 2,
                "tagNames": [],
            },
            "_file_path": f"{name}.mkv.exe" if is_lure else f"{name}.mkv",
        })
    for idx in range(350, 400):
        ih = f"{idx + 1:040x}"
        rows.append({
            "infoHash": ih,
            "contentType": "movie",
            "title": f"Historical.Movie.{idx}",
            "seeders": 12,
            "leechers": 1,
            "torrent": {
                "infoHash": ih,
                "name": f"Historical.Movie.{idx}.1080p",
                "size": 3_000_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 12,
                "leechers": 1,
                "tagNames": ["armiger-movie", "armiger-lang-english"],
            },
            "_file_path": f"Historical.Movie.{idx}.1080p.mkv",
        })

    search_offsets: List[int] = []

    def fake_transport(url: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        q = str(payload.get("query") or "")
        variables = payload.get("variables") or {}
        if "ArmigerRecentTorrents" in q:
            inp = variables.get("input") or {}
            lim = int(inp.get("limit") or 100)
            off = int(inp.get("offset") or 0)
            search_offsets.append(off)
            page = rows[off : off + lim]
            return {
                "data": {
                    "torrentContent": {
                        "search": {
                            "hasNextPage": (off + lim) < len(rows),
                            "items": page,
                        }
                    }
                }
            }
        if "ArmigerTorrentFiles" in q:
            req_hashes = set((variables.get("input") or {}).get("infoHashes") or [])
            file_items = [
                {"infoHash": r["infoHash"], "path": r["_file_path"], "size": r["torrent"]["size"]}
                for r in rows
                if r["infoHash"] in req_hashes
            ]
            return {"data": {"torrent": {"files": {"items": file_items}}}}
        if "TorrentDelete" in q:
            del_hashes = set(variables.get("infoHashes") or [])
            rows[:] = [r for r in rows if r["infoHash"] not in del_hashes]
            return {"data": {"torrent": {"delete": None}}}
        if "TorrentPutTags" in q:
            tag_hashes = set(variables.get("infoHashes") or [])
            new_tags = list(variables.get("tagNames") or [])
            for r in rows:
                if r["infoHash"] in tag_hashes:
                    existing = list(r["torrent"].get("tagNames") or [])
                    r["torrent"]["tagNames"] = list(dict.fromkeys(existing + new_tags))
            return {"data": {"torrent": {"putTags": None}}}
        return {"data": {}}

    connector = BitmagnetConnector(SyntheticArmigerClassifier(), graphql_transport=fake_transport)

    # 4 consecutive 5-second polls with batch_size=100 must drain all 350 new items (2 deleted, 348 tagged)
    r1 = connector.sync_once(limit=100, apply_mutations=True)
    r2 = connector.sync_once(limit=100, apply_mutations=True)
    r3 = connector.sync_once(limit=100, apply_mutations=True)
    r4 = connector.sync_once(limit=100, apply_mutations=True)

    assert (r1["total"], r2["total"], r3["total"], r4["total"]) == (100, 100, 100, 50)
    assert r1["deletedCount"] == 2
    assert len(rows) == 398
    assert all(
        any(t.startswith("armiger-") for t in r["torrent"]["tagNames"])
        for r in rows
    )

    # Once the 350-item wave hits the historical tagged frontier on Poll 4, Poll 5 does a single offset=0 check
    search_offsets.clear()
    r5 = connector.sync_once(limit=100, apply_mutations=True)
    assert r5["total"] == 0
    assert search_offsets == [0]


def test_bitmagnet_http_server_endpoint() -> None:
    connector = BitmagnetConnector(SyntheticArmigerClassifier())
    srv = create_bitmagnet_server(connector, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok"

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/bitmagnet/classify",
            data=json.dumps({
                "items": [{
                    "infoHash": "cccc333333333333333333333333333333333333",
                    "name": "Open.Goldberg.Variations.FLAC",
                    "size": 650_000_000,
                    "seeders": 12,
                }]
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            assert body["total"] == 1
            assert body["actions"][0]["contentType"] == "music"
    finally:
        srv.shutdown()
        srv.server_close()


# =====================================================================
# 2. Stock Upstream Rats Search Connector Tests
# =====================================================================

def test_rats_row_id_from_hash_matches_cpp_implementation() -> None:
    # All-zero top 64 bits must nudge 0 -> 1 just like src/data/row_id.cpp
    assert row_id_from_hash("0000000000000000111122223333444455556666") == 1
    assert row_id_from_hash("0000000000000001111122223333444455556666") == 1
    # Case-insensitive 64-bit hex parse >> 1
    h_lower = "fedcba98765432100123456789abcdef01234567"
    h_upper = h_lower.upper()
    assert row_id_from_hash(h_lower) == row_id_from_hash(h_upper)
    assert row_id_from_hash(h_lower) == (int("fedcba9876543210", 16) >> 1)
    assert row_id_from_hash("short") == 0
    assert row_id_from_hash("zzzzzzzzzzzzzzzz111122223333444455556666") == 0


def test_rats_enum_mapping_covers_all_categories() -> None:
    assert map_to_rats_enums({"category": "movie", "adult": "non_adult", "anime": "non_anime"}, "Sintel.2010.1080p") == (
        RatsContentType.VIDEO,
        RatsContentCategory.MOVIE,
    )
    assert map_to_rats_enums(
        {"category": "movie", "adult": "non_adult", "anime": "anime"},
        "Sintel.Documentary.Behind.The.Scenes.2010.BBC.1080p",
    ) == (
        RatsContentType.VIDEO,
        RatsContentCategory.DOCUMENTARY,
    )
    assert map_to_rats_enums({"category": "tv_show", "adult": "non_adult", "anime": "non_anime"}, "Caminandes.S01E01") == (
        RatsContentType.VIDEO,
        RatsContentCategory.SERIES,
    )
    assert map_to_rats_enums({"category": "tv_show", "adult": "non_adult", "anime": "anime"}, "Open.Anime.S01E01") == (
        RatsContentType.VIDEO,
        RatsContentCategory.ANIME,
    )
    assert map_to_rats_enums({"category": "music", "adult": "non_adult", "anime": "non_anime"}) == (
        RatsContentType.AUDIO,
        RatsContentCategory.MUSIC,
    )
    assert map_to_rats_enums({"category": "ebook", "adult": "non_adult", "anime": "non_anime"}) == (
        RatsContentType.BOOKS,
        RatsContentCategory.EBOOK,
    )
    assert map_to_rats_enums({"category": "comic", "adult": "non_adult", "anime": "non_anime"}) == (
        RatsContentType.BOOKS,
        RatsContentCategory.COMICS,
    )
    assert map_to_rats_enums({"category": "software", "adult": "non_adult", "anime": "non_anime"}) == (
        RatsContentType.SOFTWARE,
        RatsContentCategory.SOFTWARE,
    )
    assert map_to_rats_enums({"category": "game", "adult": "non_adult", "anime": "non_anime"}) == (
        RatsContentType.GAMES,
        RatsContentCategory.GAME,
    )
    assert map_to_rats_enums({"category": "xxx", "adult": "adult", "anime": "non_anime"}) == (
        RatsContentType.BAD,
        RatsContentCategory.XXX,
    )


def test_rats_sync_recent_and_collision_safe_manticore_updates() -> None:
    rest_calls: List[str] = []
    sql_calls: List[str] = []

    h_movie = "1000000000000000111111111111111111111111"
    h_malware = "2000000000000000222222222222222222222222"
    h_collided = "3000000000000000333333333333333333333333"
    h_already = "4000000000000000444444444444444444444444"

    def fake_http_get(url: str) -> Any:
        rest_calls.append(url)
        if "/api/search.recent" in url:
            return [
                # String contentCategory="" matches `src/domain/torrent_codec.cpp` when Unknown
                {"hash": h_movie, "name": "Sintel.2010.1080p", "size": 4_000_000_000, "files": 1, "contentCategory": "", "seeders": 25},
                {"hash": h_malware, "name": "Fake.Movie.2024.1080p", "size": 12_000_000, "files": 1, "contentCategory": "", "seeders": 80},
                {"hash": h_collided, "name": "Caminandes.S01E01.1080p", "size": 800_000_000, "files": 1, "contentCategory": "", "seeders": 15},
                # Already classified as "movie" in `torrent_codec.cpp` -> must be skipped without ValueError!
                {"hash": h_already, "name": "Already.Classified.2020.1080p", "size": 900_000_000, "files": 1, "contentCategory": "movie", "seeders": 10},
            ]
        if f"hash={h_movie}" in url:
            return {"hash": h_movie, "files_list": [{"path": "Sintel.2010.1080p.mkv", "size": 4_000_000_000}]}
        if f"hash={h_malware}" in url:
            return {"hash": h_malware, "files_list": [{"path": "Fake.Movie.2024.1080p.mkv.exe", "size": 12_000_000}]}
        if f"hash={h_collided}" in url:
            return None  # Simulate HTTP 400 / not found on collided slot
        return {"removed": True}

    def fake_sql_executor(sql: str) -> List[Dict[str, Any]]:
        sql_calls.append(sql)
        if sql.startswith("SELECT hash FROM torrents"):
            if str(row_id_from_hash(h_movie)) in sql:
                return [{"hash": h_movie}]
            # Simulate a 63-bit rowId collision where another torrent owns h_collided's slot
            if str(row_id_from_hash(h_collided)) in sql:
                return [{"hash": "3000000000000000999999999999999999999999"}]
        return []

    connector = RatsSearchConnector(
        SyntheticArmigerClassifier(),
        http_get_json=fake_http_get,
        sql_executor=fake_sql_executor,
    )
    res = connector.sync_recent_once(limit=20, enrich_files=True, apply_changes=True)
    assert res["total"] == 3
    assert res["removedCount"] == 1
    assert res["deleteHashes"] == [h_malware]
    assert res["applied"]["removed"] == 1
    assert res["applied"]["updated"] == 1
    assert res["applied"]["collisionsSkipped"] == 1
    assert any("/api/torrent.remove?hashes=" in c for c in rest_calls)
    assert any(
        f"UPDATE torrents SET contentType = 1, contentCategory = 1 WHERE id = {row_id_from_hash(h_movie)}" in s
        for s in sql_calls
    )


def test_pure_python_mysql41_wire_protocol_client() -> None:
    """Verify `execute_manticore_mysql41` against a local TCP socket speaking MySQL 4.1 wire protocol."""
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(1)
    port = srv_sock.getsockname()[1]

    def _pkt(seq: int, payload: bytes) -> bytes:
        n = len(payload)
        return bytes([n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, seq & 0xFF]) + payload

    def _lenstr(s: str) -> bytes:
        b = s.encode("utf-8")
        return bytes([len(b)]) + b

    def server_worker() -> None:
        conn, _ = srv_sock.accept()
        with conn:
            # 1. Send greeting packet (seq=0)
            conn.sendall(_pkt(0, b"\x0a5.0.0-manticore\x00" + b"\x00" * 30))
            # 2. Read handshake response
            conn.recv(1024)
            # 3. Send OK packet (seq=2)
            conn.sendall(_pkt(2, b"\x00\x00\x00\x02\x00\x00\x00"))
            # 4. Read COM_QUERY
            conn.recv(1024)
            # 5. Send 1-column result set for `SELECT hash FROM torrents`
            conn.sendall(_pkt(1, b"\x01"))
            col_def = (
                _lenstr("def")
                + _lenstr("")
                + _lenstr("torrents")
                + _lenstr("torrents")
                + _lenstr("hash")
                + _lenstr("hash")
                + b"\x0c\x21\x00\xff\x00\x00\x00\xfd\x00\x00\x00\x00\x00"
            )
            conn.sendall(_pkt(2, col_def))
            conn.sendall(_pkt(3, b"\xfe\x00\x00\x02\x00"))
            conn.sendall(_pkt(4, _lenstr("a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4")))
            conn.sendall(_pkt(5, b"\xfe\x00\x00\x02\x00"))

    th = threading.Thread(target=server_worker, daemon=True)
    th.start()
    try:
        rows = execute_manticore_mysql41("127.0.0.1", port, "SELECT hash FROM torrents WHERE id = 1 LIMIT 1")
        assert rows == [{"hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"}]
    finally:
        srv_sock.close()


def test_rats_websocket_event_handler_and_http_server() -> None:
    h_anime = "4000000000000000444444444444444444444444"

    def fake_http_get(url: str) -> Any:
        return {
            "hash": h_anime,
            "name": "Open.Anime.Series.S01E02.1080p",
            "size": 950_000_000,
            "files": 1,
            "seeders": 40,
            "files_list": [{"path": "Open.Anime.Series.S01E02.mkv", "size": 950_000_000}],
        }

    connector = RatsSearchConnector(SyntheticArmigerClassifier(), http_get_json=fake_http_get)
    ws_res = connector.handle_websocket_event(
        json.dumps({"event": "filesReady", "data": {"hash": h_anime}}),
        apply_changes=False,
    )
    assert ws_res is not None
    assert ws_res["actions"][0]["contentType"] == int(RatsContentType.VIDEO)
    assert ws_res["actions"][0]["contentCategory"] == int(RatsContentCategory.ANIME)

    srv = create_rats_server(connector, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["connector"] == "rats-search"

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/rats-search/classify",
            data=json.dumps({
                "items": [{
                    "hash": "5000000000000000555555555555555555555555",
                    "name": "Pepper.and.Carrot.Episode.01.cbz",
                    "size": 45_000_000,
                    "seeders": 9,
                }]
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            assert body["actions"][0]["contentType"] == int(RatsContentType.BOOKS)
            assert body["actions"][0]["contentCategory"] == int(RatsContentCategory.COMICS)
    finally:
        srv.shutdown()
        srv.server_close()


def test_bitmagnet_torznab_xml_filtering_and_v3_title_language() -> None:
    connector = BitmagnetConnector(
        SyntheticArmigerClassifier(),
        require_english_audio=False,
        reject_dead_swarms=True,
    )
    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Bitmagnet Torznab</title>
    <item>
      <title>Sintel.2010.1080p.BluRay.x264</title>
      <size>4200000000</size>
      <torznab:attr name="infohash" value="1111111111111111111111111111111111111111"/>
      <torznab:attr name="seeders" value="25"/>
      <torznab:attr name="peers" value="28"/>
    </item>
    <item>
      <title>Fake.Malware.Lure.2025.1080p.mkv.exe</title>
      <size>14000000</size>
      <torznab:attr name="infohash" value="2222222222222222222222222222222222222222"/>
      <torznab:attr name="seeders" value="90"/>
      <torznab:attr name="peers" value="90"/>
    </item>
    <item>
      <title>Dead.Swarm.Movie.2019.1080p.mkv</title>
      <size>2100000000</size>
      <torznab:attr name="infohash" value="3333333333333333333333333333333333333333"/>
      <torznab:attr name="seeders" value="0"/>
      <torznab:attr name="peers" value="0"/>
    </item>
  </channel>
</rss>"""
    filtered_xml, stats = connector.filter_torznab_xml(sample_xml)
    assert stats["total"] == 3
    assert stats["admitted"] == 1
    assert stats["blocked"] == 2
    assert stats["blocked_reasons"].get("disguised_executable") == 1
    assert stats["blocked_reasons"].get("dead_swarm_blocked") == 1
    decoded = filtered_xml.decode("utf-8")
    assert "Sintel.2010.1080p.BluRay.x264" in decoded
    assert "Fake.Malware.Lure" not in decoded
    assert "armiger_category" in decoded
    assert "armiger_title_lang" in decoded


def test_torznab_numeric_files_attr_is_a_count_not_a_path() -> None:
    item = ET.fromstring(
        b"""<item xmlns:torznab="http://torznab.com/schemas/2015/feed">
      <title>Sintel.2010.1080p.BluRay.x264</title>
      <size>4200000000</size>
      <torznab:attr name="files" value="36"/>
      <torznab:attr name="seeders" value="25"/>
    </item>"""
    )
    row = BitmagnetConnector._extract_torznab_item_row(item)
    assert row["files"] == [{"path": "Sintel.2010.1080p.BluRay.x264.mkv", "size": 4_200_000_000}]


def test_turnkey_docker_compose_files_have_no_broken_host_binds_or_missing_deps() -> None:
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    compose_paths = [
        repo_root / "bitmagnet" / "docker-compose.yml",
        repo_root / "rats-search" / "docker-compose.yml",
        repo_root / "arr-stack" / "docker-compose.yml",
    ]
    for cpath in compose_paths:
        assert cpath.is_file(), f"Missing compose file: {cpath}"
        raw = cpath.read_text(encoding="utf-8")
        # Must not require relative ../armiger or ../.. host bind mounts
        assert "../armiger" not in raw, f"{cpath} still references ../armiger"
        assert "../..:/app" not in raw, f"{cpath} still references ../..:/app"
        # Must install cryptography, onnxruntime, numpy, huggingface_hub
        for pkg in ("numpy", "onnxruntime", "cryptography", "huggingface_hub"):
            assert pkg in raw, f"{cpath} missing required package {pkg}"
        # Must not bind container HTTP server to localhost when publishing ports
        assert 'ARMIGER_BRIDGE_HOST: "localhost"' not in raw


def test_installed_armiger_loader_uses_verified_data_without_cache_imports(monkeypatch, tmp_path):
    """A model cache must not become an executable Python search path."""
    import sys
    import types
    import bitmagnet_connector as module

    calls = []
    package = types.ModuleType("armiger")
    artifacts = types.ModuleType("armiger.artifacts")

    def download(directory, **kwargs):
        calls.append((directory, kwargs))
        return tmp_path

    class Pipeline:
        @staticmethod
        def from_directory(directory, **kwargs):
            return directory, kwargs

    artifacts.download_model = download
    package.ArmigerUltraPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "armiger", package)
    monkeypatch.setitem(sys.modules, "armiger.artifacts", artifacts)
    before = list(sys.path)
    result = module.load_armiger_classifier(tmp_path, auto_download=False)
    assert calls == [(tmp_path, {"variant": "ultra", "offline": True})]
    assert result[0] == tmp_path
    assert result[1]["opt_in_sharing"] is None
    assert result[1]["rsi_mode"] is None
    assert sys.path == before

    bm_conn = BitmagnetConnector(SyntheticArmigerClassifier())
    bm_health = bm_conn.health_payload()
    for key in ("active_seq", "active_bundle_merkle_root", "model_source", "last_dht_check_ts", "sharing_status"):
        assert key in bm_health

    rs_conn = RatsSearchConnector(SyntheticArmigerClassifier())
    rs_health = rs_conn.health_payload()
    for key in ("active_seq", "active_bundle_merkle_root", "model_source", "last_dht_check_ts", "sharing_status"):
        assert key in rs_health


def test_bitmagnet_torznab_category_promotion_for_unknown_and_other_items() -> None:
    """Torznab items returned by Bitmagnet as `Unknown` / `8000` (`Other`) must be promoted to their Armiger Torznab category."""
    from bitmagnet_connector import TORZNAB_NS

    connector = BitmagnetConnector(
        SyntheticArmigerClassifier(),
        require_english_audio=False,
        reject_dead_swarms=True,
    )
    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Bitmagnet Torznab</title>
    <item>
      <title>Caminandes.Season.01.Complete.1080p.WEBRip</title>
      <category>Unknown</category>
      <size>3200000000</size>
      <torznab:attr name="infohash" value="aaaa111111111111111111111111111111111111"/>
      <torznab:attr name="category" value="8000"/>
      <torznab:attr name="seeders" value="18"/>
      <torznab:attr name="peers" value="20"/>
    </item>
    <item>
      <title>Sintel.Open.Movie.1080p.BluRay.x264</title>
      <category>Unknown</category>
      <size>4200000000</size>
      <torznab:attr name="infohash" value="bbbb222222222222222222222222222222222222"/>
      <torznab:attr name="category" value="8000"/>
      <torznab:attr name="seeders" value="25"/>
      <torznab:attr name="peers" value="28"/>
    </item>
    <item>
      <title>Freedoom.Phase.1.Linux.tar</title>
      <category>Other</category>
      <size>850000000</size>
      <torznab:attr name="infohash" value="cccc333333333333333333333333333333333333"/>
      <torznab:attr name="category" value="8000"/>
      <torznab:attr name="seeders" value="14"/>
      <torznab:attr name="peers" value="16"/>
    </item>
    <item>
      <title>Tears.of.Steel.2012.1080p.BluRay</title>
      <category>Movies/HD</category>
      <size>3900000000</size>
      <torznab:attr name="infohash" value="dddd444444444444444444444444444444444444"/>
      <torznab:attr name="category" value="2040"/>
      <torznab:attr name="seeders" value="30"/>
      <torznab:attr name="peers" value="35"/>
    </item>
  </channel>
</rss>"""

    filtered_xml, stats = connector.filter_torznab_xml(sample_xml)
    assert stats["total"] == 4
    assert stats["admitted"] == 4
    root = ET.fromstring(filtered_xml)
    items = root.findall("./channel/item")
    assert len(items) == 4

    def _cat_info(elem: ET.Element) -> tuple[str, str]:
        cat_text = elem.findtext("category") or ""
        cat_attr = ""
        for attr in elem.findall(f"{{{TORZNAB_NS}}}attr"):
            if attr.attrib.get("name") == "category":
                cat_attr = attr.attrib.get("value") or ""
        return cat_text, cat_attr

    # Unknown TV show -> TV show / 5000
    assert _cat_info(items[0]) == ("TV show", "5000")
    # Unknown Movie -> Movie / 2000
    assert _cat_info(items[1]) == ("Movie", "2000")
    # Other Game -> Game / 4050
    assert _cat_info(items[2]) == ("Game", "4050")
    # Already-classified Movies/HD (2040) -> preserved as Movies/HD / 2040
    assert _cat_info(items[3]) == ("Movies/HD", "2040")


def test_bitmagnet_torznab_proxy_rescues_unknown_items_for_sonarr_radarr_prowlarr_category_queries() -> None:
    """Category-restricted Torznab queries (`t=tvsearch`, `t=movie`, `cat=5000`, `cat=2000`) must rescue `Unknown` items matching the requested category."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse
    from bitmagnet_connector import TORZNAB_NS

    tv_item_known = """
    <item>
      <title>Known.Show.S01E01.1080p.WEB</title>
      <guid>1111111111111111111111111111111111111111</guid>
      <category>TV show</category>
      <size>1500000000</size>
      <torznab:attr name="infohash" value="1111111111111111111111111111111111111111"/>
      <torznab:attr name="category" value="5000"/>
      <torznab:attr name="seeders" value="22"/>
      <torznab:attr name="peers" value="25"/>
    </item>"""

    tv_item_unknown_s01e02 = """
    <item>
      <title>Open.Series.S01E02.1080p.WEBRip</title>
      <guid>2222222222222222222222222222222222222222</guid>
      <category>Unknown</category>
      <size>1600000000</size>
      <torznab:attr name="infohash" value="2222222222222222222222222222222222222222"/>
      <torznab:attr name="category" value="8000"/>
      <torznab:attr name="seeders" value="19"/>
      <torznab:attr name="peers" value="21"/>
    </item>"""

    movie_item_unknown = """
    <item>
      <title>Open.Cinema.Feature.1080p.BluRay</title>
      <guid>3333333333333333333333333333333333333333</guid>
      <category>Unknown</category>
      <size>4100000000</size>
      <torznab:attr name="infohash" value="3333333333333333333333333333333333333333"/>
      <torznab:attr name="category" value="8000"/>
      <torznab:attr name="seeders" value="31"/>
      <torznab:attr name="peers" value="34"/>
    </item>"""

    def _wrap_rss(items_xml: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
            f"<channel><title>Bitmagnet</title>{items_xml}</channel></rss>"
        ).encode("utf-8")

    class UpstreamBitmagnetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            t_val = (params.get("t") or ["search"])[0].lower()
            cat_val = ",".join(params.get("cat") or [])
            # Stock Bitmagnet excludes content_type IS NULL rows whenever t=tvsearch/movie or cat=5000/2000 is set
            if t_val == "tvsearch" or "5000" in cat_val or "5040" in cat_val:
                body = _wrap_rss(tv_item_known)
            elif t_val == "movie" or "2000" in cat_val or "2040" in cat_val:
                body = _wrap_rss("")
            else:
                body = _wrap_rss(tv_item_known + tv_item_unknown_s01e02 + movie_item_unknown)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    upstream_srv = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamBitmagnetHandler)
    upstream_port = upstream_srv.server_address[1]
    threading.Thread(target=upstream_srv.serve_forever, daemon=True).start()

    connector = BitmagnetConnector(
        SyntheticArmigerClassifier(),
        upstream_torznab_url=f"http://127.0.0.1:{upstream_port}/torznab",
    )
    proxy_srv = create_bitmagnet_server(connector, host="127.0.0.1", port=0)
    proxy_port = proxy_srv.server_address[1]
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

    try:
        # 1. Sonarr / Prowlarr TV query (`t=tvsearch&cat=5000,5040&q=Open`) rescues tv_item_unknown_s01e02 as 5000, excludes movie_item_unknown
        with urllib.request.urlopen(
            f"http://127.0.0.1:{proxy_port}/torznab?t=tvsearch&cat=5000,5040&q=Open",
            timeout=5.0,
        ) as resp:
            tv_xml = resp.read()
            assert resp.headers["X-Armiger-Total"] == "2"
            assert resp.headers["X-Armiger-Admitted"] == "2"
            assert resp.headers["X-Armiger-Blocked"] == "0"
        tv_root = ET.fromstring(tv_xml)
        tv_titles = [it.findtext("title") for it in tv_root.findall("./channel/item")]
        assert tv_titles == [
            "Known.Show.S01E01.1080p.WEB",
            "Open.Series.S01E02.1080p.WEBRip",
        ]
        rescued_tv = tv_root.findall("./channel/item")[1]
        assert rescued_tv.findtext("category") == "TV show"
        assert any(
            a.attrib.get("name") == "category" and a.attrib.get("value") == "5000"
            for a in rescued_tv.findall(f"{{{TORZNAB_NS}}}attr")
        )

        # 2. Radarr / Prowlarr Movie query (`t=movie&cat=2000,2040&q=Open`) rescues movie_item_unknown as 2000, excludes TV items
        with urllib.request.urlopen(
            f"http://127.0.0.1:{proxy_port}/torznab?t=movie&cat=2000,2040&q=Open",
            timeout=5.0,
        ) as resp:
            movie_xml = resp.read()
            assert resp.headers["X-Armiger-Total"] == "1"
            assert resp.headers["X-Armiger-Admitted"] == "1"
            assert resp.headers["X-Armiger-Blocked"] == "0"
        movie_root = ET.fromstring(movie_xml)
        movie_items = movie_root.findall("./channel/item")
        assert len(movie_items) == 1
        assert movie_items[0].findtext("title") == "Open.Cinema.Feature.1080p.BluRay"
        assert movie_items[0].findtext("category") == "Movie"
        assert any(
            a.attrib.get("name") == "category" and a.attrib.get("value") == "2000"
            for a in movie_items[0].findall(f"{{{TORZNAB_NS}}}attr")
        )

        # 3. Season/episode filter (`season=1&ep=9`) does not rescue S01E02 when episode mismatches
        with urllib.request.urlopen(
            f"http://127.0.0.1:{proxy_port}/torznab?t=tvsearch&cat=5000&q=Open&season=1&ep=9",
            timeout=5.0,
        ) as resp:
            ep_xml = resp.read()
        ep_titles = [it.findtext("title") for it in ET.fromstring(ep_xml).findall("./channel/item")]
        assert "Open.Series.S01E02.1080p.WEBRip" not in ep_titles
    finally:
        proxy_srv.shutdown()
        proxy_srv.server_close()
        upstream_srv.shutdown()
        upstream_srv.server_close()


def test_bitmagnet_sync_and_repair_unknown_content_types_via_import_endpoint() -> None:
    """`sync_once` and `repair_unknown_content_types` must write `contentType` hints to Bitmagnet `/import` for `Unknown` torrents."""
    rows: List[Dict[str, Any]] = [
        # Already tagged `armiger-tv-show` with contentType=None (the exact state reported by users upgrading)
        {
            "infoHash": f"{101:040x}",
            "contentType": None,
            "title": "Already Tagged TV Show",
            "seeders": 14,
            "leechers": 2,
            "torrent": {
                "infoHash": f"{101:040x}",
                "name": "Caminandes.Season.01.1080p",
                "size": 2_500_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 14,
                "leechers": 2,
                "tagNames": ["armiger-tv-show", "armiger-lang-english"],
            },
        },
        # Newly crawled untagged movie with contentType=None
        {
            "infoHash": f"{102:040x}",
            "contentType": None,
            "title": "Untagged Movie",
            "seeders": 25,
            "leechers": 3,
            "torrent": {
                "infoHash": f"{102:040x}",
                "name": "Sintel.Open.Film.1080p",
                "size": 4_100_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": 25,
                "leechers": 3,
                "tagNames": [],
            },
        },
        # Synthetic sourceless fixture (item.seeders=20, torrent.seeders=None) -> must NOT be sent to /import
        {
            "infoHash": f"{103:040x}",
            "contentType": None,
            "title": "Synthetic Seeded Fixture",
            "seeders": 20,
            "leechers": 2,
            "torrent": {
                "infoHash": f"{103:040x}",
                "name": "Tears.of.Steel.2012.1080p.mkv",
                "size": 2_400_000_000,
                "filesCount": 1,
                "hasFilesInfo": False,
                "seeders": None,
                "leechers": None,
                "tagNames": ["armiger-movie"],
            },
        },
    ]

    imported_batches: List[List[Dict[str, Any]]] = []

    def fake_graphql(url: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        q = str(payload.get("query") or "")
        variables = payload.get("variables") or {}
        if "ArmigerRecentTorrents" in q:
            inp = variables.get("input") or {}
            facets = inp.get("facets") or {}
            if "contentType" in facets:
                matched = [
                    r for r in rows
                    if r.get("contentType") is None
                    and any(t.startswith("armiger-") for t in (r["torrent"].get("tagNames") or []))
                ]
            else:
                matched = list(rows)
            return {
                "data": {
                    "torrentContent": {
                        "search": {
                            "hasNextPage": False,
                            "items": matched,
                        }
                    }
                }
            }
        if "TorrentPutTags" in q:
            tag_hashes = set(variables.get("infoHashes") or [])
            new_tags = list(variables.get("tagNames") or [])
            for r in rows:
                if r["infoHash"] in tag_hashes:
                    existing = list(r["torrent"].get("tagNames") or [])
                    r["torrent"]["tagNames"] = list(dict.fromkeys(existing + new_tags))
            return {"data": {"torrent": {"putTags": None}}}
        return {"data": {}}

    def fake_import(url: str, hints: Sequence[Mapping[str, Any]]) -> int:
        batch = [dict(h) for h in hints]
        imported_batches.append(batch)
        for h in batch:
            for r in rows:
                if r["infoHash"] == h["infoHash"]:
                    r["contentType"] = h["contentType"]
        return len(batch)

    connector = BitmagnetConnector(
        SyntheticArmigerClassifier(),
        graphql_url="http://bitmagnet:3333/graphql",
        graphql_transport=fake_graphql,
        import_transport=fake_import,
    )
    assert connector.import_url == "http://bitmagnet:3333/import"

    # 1. Explicit backfill of already-tagged Unknown torrents repairs 101 (tv_show) and skips synthetic 103
    repair_res = connector.repair_unknown_content_types(limit=100, apply_mutations=True)
    assert repair_res["repairedCount"] == 1
    assert repair_res["submittedCount"] == 1
    assert repair_res["byContentType"] == {"tv_show": 1}
    assert rows[0]["contentType"] == "tv_show"
    assert rows[2]["contentType"] is None

    # 2. sync_once classifies untagged 102 (movie) and submits its contentType hint to /import
    sync_res = connector.sync_once(limit=100, apply_mutations=True)
    assert sync_res["total"] == 1
    assert sync_res["importedHintCount"] == 1
    assert rows[1]["contentType"] == "movie"
    assert len(imported_batches) == 2
    assert imported_batches[0] == [{
        "source": "armiger",
        "infoHash": f"{101:040x}",
        "name": "Caminandes.Season.01.1080p",
        "size": 2_500_000_000,
        "contentType": "tv_show",
    }]
    assert imported_batches[1] == [{
        "source": "armiger",
        "infoHash": f"{102:040x}",
        "name": "Sintel.Open.Film.1080p",
        "size": 4_100_000_000,
        "contentType": "movie",
    }]
