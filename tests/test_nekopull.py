#!/usr/bin/env python3
"""Tests for nekopull. Run with: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nekopull  # noqa: E402
from nekopull import (  # noqa: E402
    Config,
    ConfigError,
    Defaults,
    Release,
    Show,
    State,
    Transmission,
    TransmissionConfig,
    TransmissionError,
    load_config,
    parse_episode,
    parse_release,
    passes_filters,
    rank,
    select,
    strip_tags,
)

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x"


class EpisodeParsingTest(unittest.TestCase):
    def test_sxxeyy(self):
        self.assertEqual(parse_episode("Show S01E12 Title 1080p"), (1, 12, False))
        self.assertEqual(parse_episode("Show s2e05 MULTi AAC2.0"), (2, 5, False))

    def test_proper_marker(self):
        self.assertEqual(parse_episode("Show S01E12v2 1080p"), (1, 12, True))

    def test_dash_number(self):
        self.assertEqual(
            parse_episode("[Erai-raws] Tensei Goblin dakedo Shitsumon Aru - 08 [1080p]"),
            (0, 8, False),
        )

    def test_explicit_prefix(self):
        self.assertEqual(parse_episode("[ToonsHub] Chiikawa EP382 1080p"), (0, 382, False))

    def test_nxnn(self):
        self.assertEqual(parse_episode("Show 1x07 [720p]"), (1, 7, False))

    def test_no_false_positive_on_quality(self):
        for title in (
            "Show 1080p H.264 AAC2.0",
            "Show [1080p][HEVC x265 10bit]",
            "Show E-AC3 5.1",
        ):
            self.assertEqual(parse_episode(title), (0, 0, False), title)


class TagParsingTest(unittest.TestCase):
    def test_level_extracted(self):
        clean, level = strip_tags(
            "Show - 08 [1080p] {Tags:L2;V9;C1;A=ja;S=en;}"
        )
        self.assertEqual(level, 2)
        self.assertEqual(clean, "Show - 08 [1080p]")

    def test_no_tags(self):
        clean, level = strip_tags("Show - 08 [1080p]")
        self.assertEqual(level, None)
        self.assertEqual(clean, "Show - 08 [1080p]")

    def test_release_attributes(self):
        release = parse_release(
            "[Erai-raws] Show - 08 [1080p CR WEB-DL AVC AAC][MultiSub] {Tags:L0;V9;C1;}",
            MAGNET,
        )
        self.assertEqual(release.level, 0)
        self.assertEqual(release.resolution, "1080p")
        self.assertEqual(release.codec, "h264")
        self.assertEqual(release.source, "web")
        self.assertEqual(release.group, "Erai-raws")
        self.assertEqual(release.episode, 8)
        self.assertEqual(release.infohash, "0123456789abcdef0123456789abcdef01234567")
        self.assertFalse(release.proper)

    def test_group_suffix_and_batch(self):
        release = parse_release(
            "[ToonsHub] Show S01 [1080p] (Batch) {Tags:L1;V9;C1;}", MAGNET
        )
        self.assertTrue(release.batch)
        self.assertEqual(release.group, "ToonsHub")


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.settings = Defaults(max_level=1, resolutions=["1080p"], min_seeders=0)

    def test_level_and_resolution(self):
        good = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET)
        hd = parse_release("Show S01E01 720p {Tags:L0;}", MAGNET)
        bad_level = parse_release("Show S01E01 1080p {Tags:L3;}", MAGNET)
        self.assertTrue(passes_filters(good, self.settings))
        self.assertFalse(passes_filters(hd, self.settings))
        self.assertFalse(passes_filters(bad_level, self.settings))

    def test_min_seeders(self):
        strict = Defaults(resolutions=["1080p"], min_seeders=10)
        quiet = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=2)
        busy = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=40)
        self.assertFalse(passes_filters(quiet, strict))
        self.assertTrue(passes_filters(busy, strict))

    def test_batch_and_proper(self):
        batch = parse_release("Show S01 Batch 1080p {Tags:L0;}", MAGNET)
        proper = parse_release("Show S01E01v2 1080p {Tags:L0;}", MAGNET)
        self.assertFalse(passes_filters(batch, self.settings))
        self.assertFalse(passes_filters(proper, self.settings))

    def test_prefers_lower_level(self):
        l1 = parse_release("Show S01E01 1080p {Tags:L1;}", MAGNET, seeders=100)
        l0 = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=1)
        self.assertGreater(rank(l1, self.settings), rank(l0, self.settings))

    def test_group_preference_wins_ties(self):
        settings = Defaults(resolutions=["1080p"], groups=["Erai-raws"])
        other = parse_release("[ToonsHub] Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=50)
        wanted = parse_release("[Erai-raws] Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=1)
        self.assertLess(rank(wanted, settings), rank(other, settings))


class SelectTest(unittest.TestCase):
    def test_one_per_episode_best_wins(self):
        settings = Defaults(resolutions=["1080p"])
        releases = [
            parse_release("[A] Show S01E01 1080p {Tags:L1;}", MAGNET, seeders=5),
            parse_release("[B] Show S01E01 1080p {Tags:L0;}", MAGNET, seeders=1),
            parse_release("[C] Show S01E02 1080p {Tags:L0;}", MAGNET, seeders=9),
        ]
        picks = select(releases, settings)
        self.assertEqual(len(picks), 2)
        self.assertEqual([release.episode for release in picks], [1, 2])
        self.assertEqual(picks[0].group, "B")

    def test_unparsed_episodes_dropped(self):
        settings = Defaults(resolutions=["1080p"])
        picks = select([parse_release("Show 1080p {Tags:L0;}", MAGNET)], settings)
        self.assertEqual(picks, [])

    def test_state_skips_known_episode(self):
        settings = Defaults(resolutions=["1080p"])
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.db")
            release = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET)
            self.assertEqual(len(select([release], settings, media_id="s1", state=state)), 1)
            state.record("s1", release)
            self.assertEqual(select([release], settings, media_id="s1", state=state), [])
            self.assertTrue(state.has_release(release.infohash))
            info = State(Path(tmp) / "state.db")
            self.assertTrue(info.has_episode("s1", 1, 1))
            info.close()
            state.close()


class ConfigTest(unittest.TestCase):
    def _write(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / "config.toml"
        path.write_text(body)
        return path

    def test_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nname = "one"\n',
            )
            config = load_config(path)
            self.assertEqual(config.apikey, "k")
            self.assertEqual(config.shows[0].media_id, "s1")
            self.assertEqual(config.state_path, path.parent / "state.db")

    def test_show_overrides_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                'apikey = "k"\n\n[defaults]\nmax_level = 2\n\n'
                '[[show]]\nmedia_id = "s1"\nmax_level = 0\n',
            )
            config = load_config(path)
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertEqual(merged.max_level, 0)
            self.assertEqual(merged.resolutions, ["1080p"])

    def test_missing_apikey(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, '[[show]]\nmedia_id = "s1"\n')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ntitle = "x"\n')
            with self.assertRaises(ConfigError):
                load_config(path)


class FeedParsingTest(unittest.TestCase):
    def test_items_parsed(self):
        xml = b"""<?xml version="1.0"?>
        <rss version="1.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
          <channel>
            <item>
              <title>[A] Show S01E03 1080p {Tags:L0;V9;}</title>
              <link>magnet:?xt=urn:btih:aaaabbbbccccddddeeeeffff0000111122223333</link>
              <torznab:attr name="seeders" value="7"/>
              <torznab:attr name="size" value="1234"/>
            </item>
          </channel>
        </rss>"""
        original = nekopull.http_get
        nekopull.http_get = lambda url, **kwargs: xml
        try:
            releases = nekopull.fetch_releases("key", "s1")
        finally:
            nekopull.http_get = original
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0].episode, 3)
        self.assertEqual(releases[0].seeders, 7)
        self.assertEqual(releases[0].size, 1234)


class _TransmissionHandler(http.server.BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):  # noqa: N802
        type(self).calls += 1
        if type(self).calls == 1:
            self.send_response(409)
            self.send_header("X-Transmission-Session-Id", "session-1")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if payload["method"] == "session-get":
            body = {"result": "success", "arguments": {"version": "4.0.6"}}
        else:
            body = {"result": "success", "arguments": {"torrent-added": {"id": 1}}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TransmissionTest(unittest.TestCase):
    def setUp(self):
        _TransmissionHandler.calls = 0
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _TransmissionHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = TransmissionConfig(url=f"http://127.0.0.1:{port}/transmission/rpc")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_session_handshake_and_add(self):
        client = Transmission(self.config)
        self.assertEqual(client.version(), "4.0.6")
        self.assertEqual(client.add(MAGNET), "added")

    def test_unreachable(self):
        client = Transmission(TransmissionConfig(url="http://127.0.0.1:1/transmission/rpc"))
        with self.assertRaises(TransmissionError):
            client.version()


if __name__ == "__main__":
    unittest.main()
