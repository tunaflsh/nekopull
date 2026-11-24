#!/usr/bin/env python3
"""Tests for nekopull. Run with: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nekopull  # noqa: E402
from nekopull import (  # noqa: E402
    ClientConfig,
    ClientError,
    ConfigError,
    Defaults,
    State,
    Transmission,
    load_config,
    make_client,
    parse_episode,
    parse_release,
    passes_filters,
    rank,
    select,
    split_tags,
)

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x"


def write_config(tmp: str, body: str) -> Path:
    path = Path(tmp) / "config.toml"
    path.write_text(body)
    return path


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
    def test_fields_extracted(self):
        clean, tags = split_tags("Show - 08 [1080p] {Tags:L2;V9;C1;A=ja;S=en;}")
        self.assertEqual(clean, "Show - 08 [1080p]")
        self.assertEqual(tags, {"L": 2, "V": 9, "C": 1, "A": "ja", "S": "en"})

    def test_no_tags(self):
        clean, tags = split_tags("Show - 08 [1080p]")
        self.assertEqual(clean, "Show - 08 [1080p]")
        self.assertEqual(tags, {})

    def test_tag_codec_wins_over_the_title(self):
        release = parse_release("[X] Show S01E01 1080p AVC {Tags:L0;V9;C2;}", MAGNET)
        self.assertEqual(release.codec, "h265")

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
        release = parse_release("[ToonsHub] Show S01 [1080p] (Batch) {Tags:L1;V9;C1;}", MAGNET)
        self.assertTrue(release.batch)
        self.assertEqual(release.group, "ToonsHub")


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.settings = Defaults(levels=[3], resolutions=["1080p"])

    def test_level_must_be_accepted(self):
        official = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET)
        fansub = parse_release("Show S01E01 1080p {Tags:L3;}", MAGNET)
        self.assertFalse(passes_filters(official, self.settings))
        self.assertTrue(passes_filters(fansub, self.settings))

    def test_no_level_filter_accepts_everything(self):
        settings = Defaults(resolutions=["1080p"])
        official = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET)
        self.assertTrue(passes_filters(official, settings))

    def test_untagged_release_rejected_when_levels_set(self):
        untagged = parse_release("Show S01E01 1080p", MAGNET)
        self.assertFalse(passes_filters(untagged, self.settings))

    def test_resolution(self):
        hd = parse_release("Show S01E01 720p {Tags:L3;}", MAGNET)
        self.assertFalse(passes_filters(hd, self.settings))

    def test_batch(self):
        batch = parse_release("Show S01 Batch 1080p {Tags:L3;}", MAGNET)
        self.assertFalse(passes_filters(batch, self.settings))
        relaxed = Defaults(resolutions=["1080p"], allow_batch=True)
        self.assertTrue(passes_filters(batch, relaxed))

    def test_v2_is_accepted(self):
        proper = parse_release("Show S01E01v2 1080p {Tags:L3;}", MAGNET)
        self.assertTrue(passes_filters(proper, self.settings))

    def test_codec(self):
        settings = Defaults(codecs=["h265"], resolutions=["1080p"])
        hevc = parse_release("Show S01E01 1080p HEVC {Tags:L3;}", MAGNET)
        h264 = parse_release("Show S01E01 1080p x264 {Tags:L3;}", MAGNET)
        self.assertTrue(passes_filters(hevc, settings))
        self.assertFalse(passes_filters(h264, settings))

    def test_no_codec_filter_accepts_everything(self):
        settings = Defaults(resolutions=["1080p"])
        h264 = parse_release("Show S01E01 1080p x264 {Tags:L3;}", MAGNET)
        self.assertTrue(passes_filters(h264, settings))

    def test_prefers_higher_level(self):
        l0 = parse_release("Show S01E01 1080p {Tags:L0;}", MAGNET)
        l3 = parse_release("Show S01E01 1080p {Tags:L3;}", MAGNET)
        settings = Defaults(resolutions=["1080p"])
        self.assertLess(rank(l3, settings), rank(l0, settings))

    def test_group_preference_wins_ties(self):
        settings = Defaults(resolutions=["1080p"], groups=["Erai-raws"])
        other = parse_release("[ToonsHub] Show S01E01 1080p {Tags:L3;}", MAGNET)
        wanted = parse_release("[Erai-raws] Show S01E01 1080p {Tags:L3;}", MAGNET)
        self.assertLess(rank(wanted, settings), rank(other, settings))


class SelectTest(unittest.TestCase):
    def test_one_per_episode_best_wins(self):
        settings = Defaults(resolutions=["1080p"])
        releases = [
            parse_release("[A] Show S01E01 1080p {Tags:L1;}", MAGNET),
            parse_release("[B] Show S01E01 1080p {Tags:L3;}", MAGNET),
            parse_release("[C] Show S01E02 1080p {Tags:L3;}", MAGNET),
        ]
        picks = select(releases, settings)
        self.assertEqual([release.episode for release in picks], [1, 2])
        self.assertEqual(picks[0].group, "B")

    def test_other_codecs_are_recognised(self):
        for title, expected in (
            ("Show S01E01 1080p VP9", "vp9"),
            ("Show S01E01 DVD XviD", "mpeg4"),
            ("Show S01E01 MPEG-2", "mpeg2"),
            ("Show S01E01 BD VC-1", "vc1"),
        ):
            self.assertEqual(parse_release(title, MAGNET).codec, expected, title)

    def test_unparsed_episodes_dropped(self):
        settings = Defaults(resolutions=["1080p"])
        self.assertEqual(select([parse_release("Show 1080p {Tags:L3;}", MAGNET)], settings), [])

    def test_state_skips_known_episode(self):
        settings = Defaults(resolutions=["1080p"])
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.db")
            release = parse_release("Show S01E01 1080p {Tags:L3;}", MAGNET)
            self.assertEqual(len(select([release], settings, media_id="s1", state=state)), 1)
            state.record("s1", release)
            self.assertEqual(select([release], settings, media_id="s1", state=state), [])
            self.assertTrue(state.has_release(release.infohash))
            reopened = State(Path(tmp) / "state.db")
            self.assertTrue(reopened.has_episode("s1", 1, 1))
            reopened.close()
            state.close()


class ConfigTest(unittest.TestCase):
    def test_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nname = "one"\n')
            config = load_config(path, migrate=False)
            self.assertEqual(config.apikey, "k")
            self.assertEqual(config.apikey_source, "config file")
            self.assertEqual(config.shows[0].media_id, "s1")
            self.assertEqual(config.client.type, "transmission")
            self.assertEqual(config.state_path, path.parent / "state.db")

    def test_show_override_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\nlevels = [3]\n\n'
                '[[show]]\nmedia_id = "s1"\nlevels = [0, 1]\n',
            )
            config = load_config(path, migrate=False)
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertEqual(merged.levels, [0, 1])
            self.assertEqual(merged.resolutions, ["1080p"])

    def test_codec_accepts_string_or_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\ncodec = "h265"\n\n'
                '[[show]]\nmedia_id = "s1"\ncodec = ["h264", "AV1"]\n',
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.defaults.codecs, ["h265"])
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertEqual(merged.codecs, ["h264", "av1"])

    def test_codec_values_come_from_the_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ncodec = ["vp9", "mpeg2"]\n'
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.shows[0].codecs, ["vp9", "mpeg2"])

    def test_unknown_codec_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ncodec = "h266"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_client_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[client]\ntype = "transmission"\n'
                'url = "http://box:9091/transmission/rpc"\ndownload_dir = "/tv"\n\n'
                '[[show]]\nmedia_id = "s1"\n',
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.client.url, "http://box:9091/transmission/rpc")
            self.assertEqual(config.client.download_dir, "/tv")

    def test_unknown_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[client]\ntype = "deluge"\n\n[[show]]\nmedia_id = "s1"\n',
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_missing_apikey(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, '[[show]]\nmedia_id = "s1"\n')
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_unknown_show_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ntitle = "x"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_key_migrated_to_libsecret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "secret-value"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                    ) as run:
                config = load_config(path)
            self.assertEqual(config.apikey, "secret-value")
            self.assertIn("libsecret", config.apikey_source)
            self.assertEqual(run.call_args.kwargs["input"], "secret-value\n")
            self.assertIn("store", run.call_args.args[0])
            self.assertEqual(
                path.read_text(),
                'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n',
            )

    def test_migration_preserves_a_trailing_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "secret-value"  # inline\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                    ):
                load_config(path)
            self.assertIn('apikey = "libsecret"  # inline', path.read_text())

    def test_migration_skipped_for_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "secret-value"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            config = load_config(path, migrate=False)
            self.assertIn("secret-value", path.read_text())
            self.assertEqual(config.apikey_source, "config file")

    def test_migration_skipped_without_secret_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "secret-value"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with mock.patch("nekopull.secret_tool_available", return_value=False):
                config = load_config(path)
            self.assertIn("secret-value", path.read_text())
            self.assertEqual(config.apikey, "secret-value")

    def test_stub_reads_from_libsecret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="stored\n", stderr=""),
                    ):
                config = load_config(path)
            self.assertEqual(config.apikey, "stored")
            self.assertIn("libsecret", config.apikey_source)

    def test_client_credentials_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[client]\nusername = "box"\npassword = "hunter2"\n\n'
                '[[show]]\nmedia_id = "s1"\n',
            )
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                    ) as run:
                config = load_config(path)
            self.assertEqual(config.client.username, "box")
            self.assertEqual(config.client.password, "hunter2")
            self.assertEqual(run.call_count, 3)  # apikey, username, password
            text = path.read_text()
            self.assertIn('apikey = "libsecret"', text)
            self.assertIn('username = "libsecret"', text)
            self.assertIn('password = "libsecret"', text)

    def test_client_credentials_read_from_libsecret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "libsecret"\n\n[client]\nusername = "libsecret"\n'
                'password = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n',
            )
            stored = {"apikey": "key", "client-username": "box", "client-password": "hunter2"}

            def fake_run(args, **kwargs):
                return SimpleNamespace(returncode=0, stdout=stored[args[-1]] + "\n", stderr="")

            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch("nekopull.subprocess.run", side_effect=fake_run):
                config = load_config(path)
            self.assertEqual(config.apikey, "key")
            self.assertEqual(config.client.username, "box")
            self.assertEqual(config.client.password, "hunter2")

    def test_client_credential_missing_from_libsecret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[client]\npassword = "libsecret"\n\n'
                '[[show]]\nmedia_id = "s1"\n',
            )
            def fake_run(args, **kwargs):  # stores succeed, lookups miss
                code = 0 if "store" in args else 1
                return SimpleNamespace(returncode=code, stdout="", stderr="")

            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch("nekopull.subprocess.run", side_effect=fake_run):
                with self.assertRaises(ConfigError) as caught:
                    load_config(path)
            self.assertIn("client-password", str(caught.exception))

    def test_stub_without_stored_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                    ):
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


class FeedRequestTest(unittest.TestCase):
    def _url_for(self, **kwargs) -> dict:
        captured = {}

        def fake_get(url, **ignored):
            captured["url"] = url
            return b"<rss><channel></channel></rss>"

        original = nekopull.http_get
        nekopull.http_get = fake_get
        try:
            nekopull.fetch_releases("key", "s1", **kwargs)
        finally:
            nekopull.http_get = original
        return {
            key: value[0]
            for key, value in urllib.parse.parse_qs(
                urllib.parse.urlparse(captured["url"]).query
            ).items()
        }

    def test_filters_are_sent_to_the_api(self):
        query = self._url_for(levels=[2, 3], codecs=["h265", "av1"])
        self.assertEqual(query["levels"], "2,3")
        self.assertEqual(query["video_codec"], "2,3")
        self.assertEqual(query["media_id"], "s1")

    def test_unmappable_codec_keeps_the_filter_local(self):
        query = self._url_for(codecs=["h265", "mpeg4"])
        self.assertNotIn("video_codec", query)

    def test_no_filters_means_no_extra_params(self):
        query = self._url_for()
        self.assertNotIn("levels", query)
        self.assertNotIn("video_codec", query)


class TransmissionTest(unittest.TestCase):
    def setUp(self):
        _TransmissionHandler.calls = 0
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _TransmissionHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = ClientConfig(url=f"http://127.0.0.1:{port}/transmission/rpc")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_session_handshake_and_add(self):
        client = Transmission(self.config)
        self.assertEqual(client.version(), "4.0.6")
        self.assertEqual(client.add(MAGNET, tags=["show"]), "added")

    def test_unreachable(self):
        client = Transmission(ClientConfig(url="http://127.0.0.1:1/transmission/rpc"))
        with self.assertRaises(ClientError):
            client.version()


class ClientRegistryTest(unittest.TestCase):
    def test_transmission_is_registered(self):
        client = make_client(ClientConfig())
        self.assertIsInstance(client, Transmission)

    def test_unknown_backend(self):
        with self.assertRaises(ConfigError):
            make_client(ClientConfig(type="rtorrent"))


if __name__ == "__main__":
    unittest.main()
