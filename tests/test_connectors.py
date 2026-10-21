"""Unit and HTTP integration tests for `Armiger-Connectors` (`bitmagnet` & `rats-search`)."""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Mapping, Sequence
import urllib.request

from bitmagnet_connector import (
    BitmagnetConnector,
    create_server as create_bitmagnet_server,
    has_disguised_executable,
)
from rats_search_connector import (
    RatsContentCategory,
    RatsContentType,
    RatsSearchConnector,
    create_server as create_rats_server,
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

def test_disguised_executable_detection() -> None:
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

    # Both surviving movies share the exact same tag set, so they are grouped into 1 putTags mutation
    assert len(res["graphqlPutTagsMutations"]) == 1
    tag_mut = res["graphqlPutTagsMutations"][0]
    assert "putTags(infoHashes: $infoHashes, tagNames: $tagNames)" in tag_mut["query"]
    assert tag_mut["variables"]["infoHashes"] == [
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
    ]
    assert "armiger:movie" in tag_mut["variables"]["tagNames"]
    assert "armiger:lang:english" in tag_mut["variables"]["tagNames"]


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
                                        "tagNames": ["armiger:movie"],
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
        {"category": "movie", "adult": "non_adult", "anime": "non_anime"},
        "Planet.Open.Documentary.2022.1080p",
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

    def fake_http_get(url: str) -> Any:
        rest_calls.append(url)
        if "/api/search.recent" in url:
            return [
                {"hash": h_movie, "name": "Sintel.2010.1080p", "size": 4_000_000_000, "files": 1, "contentCategory": 0, "seeders": 25},
                {"hash": h_malware, "name": "Fake.Movie.2024.1080p", "size": 12_000_000, "files": 1, "contentCategory": 0, "seeders": 80},
                {"hash": h_collided, "name": "Caminandes.S01E01.1080p", "size": 800_000_000, "files": 1, "contentCategory": 0, "seeders": 15},
            ]
        if f"hash={h_movie}" in url:
            return {"hash": h_movie, "fileList": [{"path": "Sintel.2010.1080p.mkv", "size": 4_000_000_000}]}
        if f"hash={h_malware}" in url:
            return {"hash": h_malware, "fileList": [{"path": "Fake.Movie.2024.1080p.mkv.exe", "size": 12_000_000}]}
        if f"hash={h_collided}" in url:
            return {"hash": h_collided, "fileList": [{"path": "Caminandes.S01E01.1080p.mkv", "size": 800_000_000}]}
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


def test_rats_websocket_event_handler_and_http_server() -> None:
    h_anime = "4000000000000000444444444444444444444444"

    def fake_http_get(url: str) -> Any:
        return {
            "hash": h_anime,
            "name": "Open.Anime.Series.S01E02.1080p",
            "size": 950_000_000,
            "files": 1,
            "seeders": 40,
            "fileList": [{"path": "Open.Anime.Series.S01E02.mkv", "size": 950_000_000}],
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
