"""Unit and HTTP/wire-protocol integration tests for `Armiger-Connectors` (`bitmagnet` & `rats-search`)."""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, List, Mapping, Sequence
import urllib.request
import xml.etree.ElementTree as ET

from bitmagnet_connector import (
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
