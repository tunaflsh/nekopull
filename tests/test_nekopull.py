#!/usr/bin/env python3
"""Tests for nekopull. Run with: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import contextlib
import http.server
import io
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
    Release,
    State,
    Transmission,
    load_config,
    make_client,
    parse_episode,
    passes_filters,
    rank,
    release_from_item,
    select,
    split_tags,
)

HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=x"

EPISODES = {
    "546146": {
        "id": 546146, "season": 3, "episode": 7, "absolute": 55,
        "airDateUtc": "2026-11-13T14:00:00Z",
    },
    "546147": {
        "id": 546147, "season": 3, "episode": 6, "absolute": 54,
        "airDateUtc": "2026-11-06T14:00:00Z",
    },
    "546148": {"id": 546148, "season": 3, "episode": 8, "absolute": 56, "airDateUtc": None},
}

ITEM = {
    "id": 15262011588621,
    "title": "[Anarchy] Kusuriya no Hitorigoto - 55 (WEB 1080p).mkv",
    "media_id": "s1477",
    "media_episode_ids": ["546146"],
    "level": 3,
    "video_codec": 2,
    "video_type": 8,
    "batch": False,
    "filesize": "2416879361",
    "infohash": HASH,
    "magnet": "magnet:?xt=urn:btih:public",
    "private_magnet": "magnet:?xt=urn:btih:private",
    "groups": [{"name": "anarchy subs", "display_name": "Anarchy Subs"}],
}


def make_release(**kwargs) -> Release:
    defaults = {
        "title": "Show S01E01 1080p",
        "magnet": MAGNET,
        "infohash": HASH,
        "size": 100,
        "level": 0,
        "resolution": "1080p",
        "codec": "h264",
        "video_type": "web_dl",
        "group": "show",
        "season": 1,
        "episode": 1,
        "batch": False,
    }
    defaults.update(kwargs)
    return Release(**defaults)


class EpisodeFallbackTest(unittest.TestCase):
    """Title parsing is now only a fallback for torrents without episode ids."""

    def test_sxxeyy(self):
        self.assertEqual(parse_episode("Show S01E12 Title 1080p"), (1, 12, False))
        self.assertEqual(parse_episode("Show s2e05 MULTi AAC2.0"), (2, 5, False))

    def test_dash_number(self):
        self.assertEqual(
            parse_episode("[Erai-raws] Tensei Goblin dakedo Shitsumon Aru - 08 [1080p]"),
            (0, 8, False),
        )

    def test_explicit_prefix(self):
        self.assertEqual(parse_episode("[ToonsHub] Chiikawa EP382 1080p"), (0, 382, False))

    def test_no_false_positive_on_quality(self):
        for title in ("Show 1080p H.264 AAC2.0", "Show [1080p][HEVC x265 10bit]"):
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


class ReleaseParsingTest(unittest.TestCase):
    def test_episode_comes_from_the_media_map(self):
        release = release_from_item(ITEM, EPISODES)
        self.assertEqual((release.season, release.episode), (3, 7))
        self.assertEqual(release.level, 3)
        self.assertEqual(release.codec, "h265")
        self.assertEqual(release.video_type, "web_encode")
        self.assertEqual(release.group, "anarchy subs")
        self.assertEqual(release.resolution, "1080p")
        self.assertEqual(release.size, 2416879361)
        self.assertFalse(release.batch)

    def test_private_magnet_preferred(self):
        self.assertEqual(
            release_from_item(ITEM, EPISODES).magnet, "magnet:?xt=urn:btih:private"
        )
        self.assertEqual(
            release_from_item(ITEM, EPISODES, private_magnet=False).magnet,
            "magnet:?xt=urn:btih:public",
        )

    def test_a_pack_maps_to_its_season(self):
        pack = dict(ITEM, media_episode_ids=["546146", "546147"], batch=True)
        release = release_from_item(pack, EPISODES)
        self.assertEqual(release.season, 3)
        self.assertEqual(release.episode, 0)
        self.assertTrue(release.batch)

    def test_fallback_resolves_an_absolute_number(self):
        orphan = dict(ITEM, media_episode_ids=[], title="[Erai-raws] Show - 54 [1080p]")
        release = release_from_item(orphan, EPISODES)
        self.assertEqual((release.season, release.episode), (3, 6))

    def test_fallback_keeps_an_explicit_season(self):
        orphan = dict(ITEM, media_episode_ids=[], title="[X] Show S02E03 1080p")
        release = release_from_item(orphan, EPISODES)
        self.assertEqual((release.season, release.episode), (2, 3))

    def test_fallback_drops_an_unknown_absolute_number(self):
        orphan = dict(ITEM, media_episode_ids=[], title="[Erai-raws] Show - 99 [1080p]")
        release = release_from_item(orphan, EPISODES)
        self.assertEqual((release.season, release.episode), (0, 0))

    def test_unknown_episode_id_is_ignored(self):
        orphan = dict(ITEM, media_episode_ids=["999"], title="Show S02E03 1080p")
        release = release_from_item(orphan, EPISODES)
        self.assertEqual((release.season, release.episode), (2, 3))

    def test_announced_episode_is_not_taken(self):
        announced = dict(ITEM, media_episode_ids=["546148"])
        self.assertFalse(release_from_item(announced, EPISODES).aired)

    def test_unmapped_episode_past_the_last_aired_is_rejected(self):
        # e.g. "S05E14" when the season only has 12 episodes
        beyond = dict(ITEM, media_episode_ids=[], title="[X] Show S03E14 1080p")
        self.assertFalse(release_from_item(beyond, EPISODES).aired)

    def test_unmapped_episode_within_aired_range_is_accepted(self):
        # the site has not mapped it yet, but it cannot be newer than what aired
        lagging = dict(ITEM, media_episode_ids=[], title="[X] Show S03E05 1080p")
        self.assertTrue(release_from_item(lagging, EPISODES).aired)

    def test_unmapped_absolute_number_must_be_known(self):
        unknown = dict(ITEM, media_episode_ids=[], title="[X] Show - 99 [1080p]")
        self.assertFalse(release_from_item(unknown, EPISODES).aired)

    def test_anonymous_release_has_no_group(self):
        self.assertIsNone(release_from_item(dict(ITEM, groups=[]), EPISODES).group)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.settings = Defaults(levels=[3], resolutions=["1080p"])

    def test_level_must_be_accepted(self):
        self.assertFalse(passes_filters(make_release(level=0), self.settings))
        self.assertTrue(passes_filters(make_release(level=3), self.settings))

    def test_no_level_filter_accepts_everything(self):
        settings = Defaults(resolutions=["1080p"])
        self.assertTrue(passes_filters(make_release(level=0), settings))

    def test_untagged_release_rejected_when_levels_set(self):
        self.assertFalse(passes_filters(make_release(level=None), self.settings))

    def test_unaired_release_is_skipped(self):
        self.assertFalse(passes_filters(make_release(aired=False), self.settings))

    def test_resolution(self):
        self.assertFalse(passes_filters(make_release(resolution="720p"), self.settings))

    def test_codec(self):
        settings = Defaults(codecs=["h265"])
        self.assertTrue(passes_filters(make_release(codec="h265"), settings))
        self.assertFalse(passes_filters(make_release(codec="h264"), settings))

    def test_video_type(self):
        settings = Defaults(video_types=["web_dl"])
        self.assertTrue(passes_filters(make_release(video_type="web_dl"), settings))
        self.assertFalse(passes_filters(make_release(video_type="web_encode"), settings))

    def test_batch(self):
        batch = make_release(batch=True)
        self.assertFalse(passes_filters(batch, self.settings))
        relaxed = Defaults(resolutions=["1080p"], allow_batch=True)
        self.assertTrue(passes_filters(batch, relaxed))

    def test_prefers_higher_level(self):
        settings = Defaults(resolutions=["1080p"])
        self.assertLess(
            rank(make_release(level=3), settings), rank(make_release(level=0), settings)
        )

    def test_resolution_order_breaks_a_tie(self):
        settings = Defaults(resolutions=["1080p", "720p"])
        hd = make_release(resolution="1080p")
        sd = make_release(resolution="720p")
        self.assertLess(rank(hd, settings), rank(sd, settings))

    def test_group_beats_resolution(self):
        settings = Defaults(resolutions=["1080p", "720p"], groups=["erai-raws"])
        wanted = make_release(resolution="720p", group="erai-raws")
        other = make_release(resolution="1080p", group="toonshub")
        self.assertLess(rank(wanted, settings), rank(other, settings))

    def test_level_beats_type_and_codec(self):
        settings = Defaults(
            resolutions=["1080p"], video_types=["web_dl", "web_encode"], codecs=["h265", "h264"]
        )
        better_subs = make_release(level=3, video_type="web_encode", codec="h264")
        nicer_source = make_release(level=0, video_type="web_dl", codec="h265")
        self.assertLess(rank(better_subs, settings), rank(nicer_source, settings))

    def test_type_then_codec_break_ties(self):
        settings = Defaults(
            resolutions=["1080p"], video_types=["web_dl", "web_encode"], codecs=["h265", "h264"]
        )
        web = make_release(video_type="web_dl", codec="h264")
        encode = make_release(video_type="web_encode", codec="h265")
        self.assertLess(rank(web, settings), rank(encode, settings))
        self.assertLess(
            rank(make_release(codec="h265"), settings),
            rank(make_release(codec="h264"), settings),
        )

    def test_group_preference_wins_ties(self):
        settings = Defaults(resolutions=["1080p"], groups=["Erai-raws"])
        wanted = make_release(group="erai-raws")
        other = make_release(group="toonshub")
        self.assertLess(rank(wanted, settings), rank(other, settings))


class SelectTest(unittest.TestCase):
    def test_one_per_episode_best_wins(self):
        settings = Defaults(resolutions=["1080p"])
        picks = select(
            [
                make_release(episode=1, level=1, group="a"),
                make_release(episode=1, level=3, group="b"),
                make_release(episode=2, level=3, group="c"),
            ],
            settings,
        )
        self.assertEqual([release.episode for release in picks], [1, 2])
        self.assertEqual(picks[0].group, "b")

    def test_packs_are_skipped_unless_allowed(self):
        settings = Defaults(resolutions=["1080p"])
        packs = [make_release(episode=0, batch=True)]
        self.assertEqual(select(packs, settings), [])
        relaxed = Defaults(resolutions=["1080p"], allow_batch=True)
        self.assertEqual(len(select(packs, relaxed)), 1)

    def test_state_skips_known_episode(self):
        settings = Defaults(resolutions=["1080p"])
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.db")
            release = make_release(season=3, episode=7)
            self.assertEqual(len(select([release], settings, media_id="s1", state=state)), 1)
            state.record("s1", release)
            self.assertEqual(select([release], settings, media_id="s1", state=state), [])
            self.assertTrue(state.has_release(HASH))
            reopened = State(Path(tmp) / "state.db")
            self.assertTrue(reopened.has_episode("s1", 3, 7))
            reopened.close()
            state.close()


class FetchTest(unittest.TestCase):
    def _call(self, payload: dict | None = None, **kwargs):
        captured = {}

        def fake_get(url, **options):
            captured["url"] = url
            captured["headers"] = options.get("headers") or {}
            return json.dumps(
                payload
                or {"data": {"results": [ITEM], "media": {"episodes": list(EPISODES.values())}}}
            ).encode()

        original = nekopull.http_get
        nekopull.http_get = fake_get
        try:
            releases = nekopull.fetch_releases("key", "s1477", **kwargs)
        finally:
            nekopull.http_get = original
        query = {
            key: value[0]
            for key, value in urllib.parse.parse_qs(
                urllib.parse.urlparse(captured["url"]).query
            ).items()
        }
        return releases, query, captured["headers"]

    def test_episodes_resolved_from_the_media_map(self):
        releases, query, headers = self._call()
        self.assertEqual(query["media_id"], "s1477")
        self.assertEqual(query["sort_by"], "latest")
        self.assertEqual(query["batch"], "false")
        self.assertEqual(headers["Cookie"], "ssid=key")
        self.assertEqual((releases[0].season, releases[0].episode), (3, 7))

    def test_filters_are_sent_to_the_api(self):
        _, query, _ = self._call(levels=[2, 3], codecs=["h265", "av1"], video_types=["web_dl"])
        self.assertEqual(query["levels"], "2,3")
        self.assertEqual(query["video_codec"], "2,3")
        self.assertEqual(query["video_type"], "9")

    def test_private_magnet_is_configurable(self):
        _, _, _ = self._call()
        releases, _, _ = self._call(private_magnet=False)
        self.assertEqual(releases[0].magnet, "magnet:?xt=urn:btih:public")

    def test_allow_batch_drops_the_batch_filter(self):
        _, query, _ = self._call(allow_batch=True)
        self.assertNotIn("batch", query)

    def test_no_filters_means_no_extra_params(self):
        _, query, _ = self._call()
        self.assertNotIn("levels", query)
        self.assertNotIn("video_codec", query)
        self.assertNotIn("video_type", query)

    def test_api_error_is_reported(self):
        with self.assertRaises(nekopull.NekobtError):
            self._call({"error": True, "message": "nope"})


class ConfigTest(unittest.TestCase):
    def test_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            config = load_config(path, migrate=False)
            self.assertEqual(config.apikey, "k")
            self.assertEqual(config.apikey_source, "config file")
            self.assertEqual(config.shows[0].media_id, "s1")
            self.assertEqual(config.client.type, "transmission")
            self.assertTrue(config.defaults.private_magnet)
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

    def test_filters_parse_from_strings_or_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\ncodec = "h265"\n\n'
                '[[show]]\nmedia_id = "s1"\ncodec = ["h264", "AV1"]\n'
                'type = ["webdl", "BD_Remux"]\n',
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.defaults.codecs, ["h265"])
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertEqual(merged.codecs, ["h264", "av1"])
            self.assertEqual(merged.video_types, ["web_dl", "bd_remux"])

    def test_unknown_codec_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ncodec = "h266"\n')
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_begin_skips_older_episodes(self):
        settings = Defaults()
        begin = (3, 7)
        self.assertFalse(passes_filters(make_release(season=3, episode=6), settings, begin))
        self.assertTrue(passes_filters(make_release(season=3, episode=7), settings, begin))
        self.assertTrue(passes_filters(make_release(season=4, episode=1), settings, begin))

    def test_begin_is_per_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nbegin = "S03"\n\n'
                '[[show]]\nmedia_id = "s2"\nbegin = "S03E07"\n',
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.shows[0].begin, (3, 0))
            self.assertEqual(config.shows[1].begin, (3, 7))
            self.assertEqual(config.defaults.merged(config.shows[0].overrides()).levels, [])

    def test_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nname = "one"\n')
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_annotations_are_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"  # Canonical One\n'
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.annotations, {"s1": "Canonical One"})

    def test_list_falls_back_to_the_media_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            buffer = io.StringIO()
            with mock.patch("nekopull.media_title", side_effect=nekopull.NekobtError("down")), \
                    contextlib.redirect_stdout(buffer):
                self.assertEqual(nekopull.command_list(load_config(path, migrate=False)), 0)
            self.assertIn("s1", buffer.getvalue())

    def test_begin_in_defaults_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\nbegin = "S03"\n\n[[show]]\nmedia_id = "s1"\n',
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_begin_accepts_absolute_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nbegin = 55\n\n'
                '[[show]]\nmedia_id = "s2"\nbegin = "3x07"\n',
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.shows[0].begin, 55)
            self.assertEqual(config.shows[1].begin, (3, 7))

    def test_absolute_begin_filters_by_absolute_number(self):
        settings = Defaults()
        self.assertFalse(passes_filters(make_release(absolute=54), settings, 55))
        self.assertTrue(passes_filters(make_release(absolute=55), settings, 55))
        # an unmapped release has no absolute number, so it is not filtered out
        self.assertTrue(passes_filters(make_release(absolute=None), settings, 55))

    def test_invalid_begin_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nbegin = "later"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_batch_key_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\nbatch = true\n\n'
                '[[show]]\nmedia_id = "s1"\nbatch = false\n',
            )
            config = load_config(path, migrate=False)
            self.assertTrue(config.defaults.allow_batch)
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertFalse(merged.allow_batch)

    def test_list_uses_config_key_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"  # Canonical One\n'
                'type = ["web_dl"]\n'
                'batch = true\nbegin = "S01E05"\n',
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                nekopull.command_list(load_config(path, migrate=False))
            output = buffer.getvalue()
            self.assertIn("Canonical One", output)
            self.assertIn("type=['web_dl']", output)
            self.assertIn("batch=True", output)
            self.assertIn("begin=S01E05", output)

    def test_unknown_video_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\ntype = "bluray"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_private_magnet_per_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nprivate_magnet = false\n',
            )
            config = load_config(path, migrate=False)
            self.assertFalse(config.shows[0].private_magnet)
            self.assertTrue(config.defaults.private_magnet)
            merged = config.defaults.merged(config.shows[0].overrides())
            self.assertFalse(merged.private_magnet)

    def test_private_magnet_per_show_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\nprivate_magnet = "yes"\n',
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_private_magnet_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\nprivate_magnet = false\n\n'
                '[[show]]\nmedia_id = "s1"\n',
            )
            self.assertFalse(load_config(path, migrate=False).defaults.private_magnet)

    def test_private_magnet_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[defaults]\nprivate_magnet = "no"\n\n'
                '[[show]]\nmedia_id = "s1"\n',
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_unknown_top_level_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\npivat = false\n\n[[show]]\nmedia_id = "s1"\n')
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
            path = write_config(tmp, 'apikey = "secret-value"\n\n[[show]]\nmedia_id = "s1"\n')
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
                path.read_text(), 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n'
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
            self.assertEqual(run.call_count, 3)
            text = path.read_text()
            for key in ("apikey", "username", "password"):
                self.assertIn(f'{key} = "libsecret"', text)

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

            def fake_run(args, **kwargs):
                code = 0 if "store" in args else 1
                return SimpleNamespace(returncode=code, stdout="", stderr="")

            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch("nekopull.subprocess.run", side_effect=fake_run):
                with self.assertRaises(ConfigError) as caught:
                    load_config(path)
            self.assertIn("client-password", str(caught.exception))

    def test_stub_without_stored_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n')
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch(
                        "nekopull.subprocess.run",
                        return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
                    ):
                with self.assertRaises(ConfigError):
                    load_config(path)


def write_config(tmp: str, body: str) -> Path:
    path = Path(tmp) / "config.toml"
    path.write_text(body)
    return path


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


class HttpTest(unittest.TestCase):
    def test_retries_after_a_429(self):
        calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                calls.append(1)
                if len(calls) == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    return
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch("nekopull.time.sleep"):
                body = nekopull.http_get(f"http://127.0.0.1:{server.server_address[1]}/")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(body, b"ok")
        self.assertEqual(len(calls), 2)

    def test_lookup_media(self):
        payload = '{"data": {"results": [{"id": "s1", "title": "One"}]}}'
        with mock.patch("nekopull.http_get", return_value=payload.encode()):
            results = nekopull.lookup_media("one")
        self.assertEqual(results[0]["id"], "s1")

    def test_lookup_media_error(self):
        with mock.patch("nekopull.http_get", return_value=b'{"error": true, "message": "no"}'):
            with self.assertRaises(nekopull.NekobtError):
                nekopull.lookup_media("one")


class CommandsTest(unittest.TestCase):
    def _config(self, tmp: str, shows: str = '[[show]]\nmedia_id = "s1"\n'):
        return load_config(write_config(tmp, 'apikey = "k"\n\n' + shows), migrate=False)

    def test_select_shows_by_media_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                tmp,
                '[[show]]\nmedia_id = "s1"\n\n[[show]]\nmedia_id = "s2"\n',
            )
            self.assertEqual([s.media_id for s in nekopull._select_shows(config, ["s2"])], ["s2"])
            self.assertEqual([s.media_id for s in nekopull._select_shows(config, ["S1"])], ["s1"])
            with self.assertRaises(ConfigError):
                nekopull._select_shows(config, ["nope"])

    def test_dry_run_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            options = SimpleNamespace(dry_run=True, show=None)
            with mock.patch("nekopull.fetch_releases", return_value=[make_release()]), \
                    mock.patch("nekopull.make_client", side_effect=AssertionError("client used")):
                self.assertEqual(nekopull.command_run(config, options), 0)
            with State(config.state_path) as state:
                self.assertFalse(state.has_episode("s1", 1, 1))

    def test_verify_rejects_a_bad_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            client = SimpleNamespace(version=lambda: "4.0.6")
            with mock.patch("nekopull.fetch_releases", return_value=[]), \
                    mock.patch("nekopull.make_client", return_value=client), \
                    mock.patch("nekopull.api_key_valid", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nekopull.command_verify(config), 1)

    def test_verify_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            client = SimpleNamespace(version=lambda: "4.0.6")
            with mock.patch("nekopull.fetch_releases", return_value=[]), \
                    mock.patch("nekopull.make_client", return_value=client), \
                    mock.patch("nekopull.api_key_valid", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nekopull.command_verify(config), 0)
            with mock.patch("nekopull.fetch_releases", side_effect=nekopull.NekobtError("down")), \
                    mock.patch("nekopull.make_client", return_value=client), \
                    mock.patch("nekopull.api_key_valid", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nekopull.command_verify(config), 1)


class CliTest(unittest.TestCase):
    def test_parser_wires_commands(self):
        parser = nekopull.build_parser()
        self.assertEqual(parser.parse_args(["run", "--dry-run"]).dry_run, True)
        self.assertEqual(parser.parse_args(["lookup", "x"]).command, "lookup")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
            parser.parse_args(["--version"])
        self.assertEqual(exit_info.exception.code, 0)

    def test_lookup_command_prints_results(self):
        options = SimpleNamespace(query="one", limit=5)
        buffer = io.StringIO()
        with mock.patch(
            "nekopull.lookup_media",
            return_value=[{"id": "s1", "year": 2025, "title": "One"}],
        ), contextlib.redirect_stdout(buffer):
            self.assertEqual(nekopull.command_lookup(options), 0)
        self.assertIn("s1", buffer.getvalue())

    def test_lookup_command_without_matches(self):
        options = SimpleNamespace(query="nope", limit=5)
        with mock.patch("nekopull.lookup_media", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nekopull.command_lookup(options), 1)

    def test_annotate_writes_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp,
                'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n\n'
                '[[show]]\n# nekoBT title: stale\nmedia_id = "s2"\nbegin = "S01E05"\n',
            )
            titles = {"s1": "Canonical One", "s2": "Canonical Two"}
            options = SimpleNamespace(config=path, dry_run=False)
            with mock.patch("nekopull.media_title", side_effect=lambda key, mid: titles[mid]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nekopull.command_annotate(load_config(path, migrate=False), options), 0)
            text = path.read_text()
            self.assertIn('media_id = "s1"  # Canonical One', text)
            self.assertIn('media_id = "s2"  # Canonical Two', text)
            self.assertNotIn("nekoBT title:", text)
            self.assertIn('begin = "S01E05"', text)

    def test_annotate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            options = SimpleNamespace(config=path, dry_run=False)
            config = load_config(path, migrate=False)
            with mock.patch("nekopull.media_title", return_value="One"), \
                    contextlib.redirect_stdout(io.StringIO()):
                nekopull.command_annotate(config, options)
                first = path.read_text()
                nekopull.command_annotate(config, options)
            self.assertEqual(path.read_text(), first)

    def test_annotate_dry_run_leaves_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            before = path.read_text()
            options = SimpleNamespace(config=path, dry_run=True)
            with mock.patch("nekopull.media_title", return_value="One"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nekopull.command_annotate(load_config(path, migrate=False), options), 0)
            self.assertEqual(path.read_text(), before)

    def test_annotate_keeps_the_line_when_the_lookup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            options = SimpleNamespace(config=path, dry_run=False)
            with mock.patch("nekopull.media_title", side_effect=nekopull.NekobtError("down")), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = nekopull.command_annotate(load_config(path, migrate=False), options)
            self.assertEqual(code, 1)
            self.assertIn('media_id = "s1"', path.read_text())

    def test_interval_comes_from_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\ninterval = "6h"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            config = load_config(path, migrate=False)
            self.assertEqual(config.interval, "6h")
            self.assertEqual(nekopull._daemon_interval(config, SimpleNamespace(interval=None)), 21600)
            self.assertEqual(nekopull._daemon_interval(config, SimpleNamespace(interval="30m")), 1800)

    def test_bad_interval_in_the_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(
                tmp, 'apikey = "k"\ninterval = "daily"\n\n[[show]]\nmedia_id = "s1"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path, migrate=False)

    def test_interval_parsing(self):
        self.assertEqual(nekopull._parse_interval("1d"), 86400)
        self.assertEqual(nekopull._parse_interval("30m"), 1800)
        self.assertEqual(nekopull._parse_interval("2h"), 7200)
        with self.assertRaises(ConfigError):
            nekopull._parse_interval("daily")

    def test_secrets_are_reused_only_when_still_stubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n')
            cache: dict[str, str] = {}
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch("nekopull.read_secret", return_value="first") as read:
                load_config(path, migrate=False, cache=cache)
                # a later reload must not hit the keyring again
                path.write_text('apikey = "libsecret"\n\n[[show]]\nmedia_id = "s2"\n')
                config = load_config(path, migrate=False, cache=cache)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(config.apikey, "first")
            self.assertEqual([s.media_id for s in config.shows], ["s2"])

    def test_a_new_literal_overrides_the_cached_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "libsecret"\n\n[[show]]\nmedia_id = "s1"\n')
            cache: dict[str, str] = {}
            with mock.patch("nekopull.secret_tool_available", return_value=True), \
                    mock.patch("nekopull.read_secret", return_value="old"):
                load_config(path, migrate=False, cache=cache)
            path.write_text('apikey = "new"\n\n[[show]]\nmedia_id = "s1"\n')
            self.assertEqual(load_config(path, migrate=False, cache=cache).apikey, "new")
            self.assertEqual(cache["apikey"], "new")

    def test_daemon_runs_then_sleeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'apikey = "k"\n\n[[show]]\nmedia_id = "s1"\n')
            config = load_config(path, migrate=False)
            options = SimpleNamespace(interval="1h", dry_run=False, config=path)
            with mock.patch("nekopull._run_once") as run, \
                    mock.patch("nekopull.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    nekopull.command_daemon(config, options)
            self.assertEqual(run.call_count, 1)

    def test_secret_tool_detection(self):
        with mock.patch("nekopull.shutil.which", return_value="/usr/bin/secret-tool"):
            self.assertTrue(nekopull.secret_tool_available())
        with mock.patch("nekopull.shutil.which", return_value=None):
            self.assertFalse(nekopull.secret_tool_available())


class ClientRegistryTest(unittest.TestCase):
    def test_transmission_is_registered(self):
        self.assertIsInstance(make_client(ClientConfig()), Transmission)

    def test_unknown_backend(self):
        with self.assertRaises(ConfigError):
            make_client(ClientConfig(type="rtorrent"))


if __name__ == "__main__":
    unittest.main()
