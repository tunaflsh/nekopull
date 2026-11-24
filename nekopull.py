#!/usr/bin/env python3
"""Watch nekoBT for new episodes of the shows you follow.

Each configured show is polled by its nekoBT media id, so a feed only ever
contains that one show. Release titles are parsed for the episode number,
resolution, codec and nekoBT's ``{Tags:Ln;...}`` sub level; nothing else about
them is trusted or needed.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

LOG = logging.getLogger("nekopull")

JSON_SEARCH_URL = "https://nekobt.to/api/v1/torrents/search"
MEDIA_SEARCH_URL = "https://nekobt.to/api/v1/media/search"
USER_AGENT = f"nekopull/{__version__}"
TORZNAB_NS = {"torznab": "http://torznab.com/schemas/2015/feed"}

CONFIG_PATH = Path(
    os.environ.get(
        "NEKOPULL_CONFIG",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nekopull/config.toml",
    )
)
STATE_PATH = Path(
    os.environ.get(
        "NEKOPULL_STATE",
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nekopull/state.db",
    )
)

# --- release title parsing -------------------------------------------------

_TAGS = re.compile(r"\s*\{Tags:([^}]*)\}\s*$")
_EP_SXXEYY = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,3})(?:v(\d+))?\b")
_EP_NXNN = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})(?:v(\d+))?\b")
_EP_PREFIXED = re.compile(r"(?i)\bEP?(\d{1,4})(?:v(\d+))?\b")
_EP_DASH = re.compile(r"(?:^|\s)-\s(\d{1,4})(?:v(\d+))?(?=[\s\[(]|$)")
_RESOLUTION = re.compile(r"(?i)\b(2160p|1440p|1080p|720p|576p|480p)\b")



@dataclass(slots=True)
class Release:
    """A single release from a feed."""

    title: str
    magnet: str
    infohash: str
    size: int = 0
    level: int | None = None
    resolution: str | None = None
    codec: str | None = None
    video_type: str | None = None
    group: str | None = None
    season: int = 0
    episode: int = 0
    absolute: int | None = None
    batch: bool = False

    @property
    def episode_key(self) -> tuple[int, int]:
        return (self.season, self.episode)


# nekoBT's video codec ids, taken from the site's own enum (the frontend bundle
# defines H264=1, H265=2, AV1=3, VP9=4, MPEG2=5, XVID=6, WMV=7, VC1=8,
# Other=0). Probing ?video_codec=N only reveals the ids that have releases, so
# it under-reports: XviD/WMV/VC1 exist but are unused. "other" has no useful
# meaning here, so it is not accepted.
_CODEC_IDS = {
    "h264": 1,
    "h265": 2,
    "av1": 3,
    "vp9": 4,
    "mpeg2": 5,
    "xvid": 6,
    "wmv": 7,
    "vc1": 8,
}
_CODEC_BY_INDEX = {index: name for name, index in _CODEC_IDS.items()}

# The site's release-type enum ("video_type" in the API), also from the frontend
# bundle. 10 is not used.
_VIDEO_TYPES = {
    "other": 0,
    "vhs": 1,
    "laserdisc": 2,
    "tv_encode": 3,
    "tv_raw": 4,
    "dvd_raw": 5,
    "dvd_encode": 6,
    "web_mini": 7,
    "web_encode": 8,
    "web_dl": 9,
    "bd_disc": 11,
    "bd_mini": 12,
    "bd_encode": 13,
    "bd_remux": 14,
    "hybrid": 15,
    "dvd_disc": 16,
}
_VIDEO_TYPE_BY_INDEX = {index: name for name, index in _VIDEO_TYPES.items()}


def _slug(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).lower()


def split_tags(title: str) -> tuple[str, dict]:
    """Split nekoBT's trailing ``{Tags:L0;V9;C1;A=ja;S=en;}`` block off a title.

    Returns the title without the block and a dict of the parsed fields, e.g.
    ``{"L": 0, "V": 9, "C": 1, "A": "ja", "S": "en"}``.
    """
    match = _TAGS.search(title)
    if not match:
        return title.strip(), {}
    tags: dict = {}
    for part in match.group(1).split(";"):
        part = part.strip()
        if not part:
            continue
        flag = re.fullmatch(r"([A-Za-z]+)(\d+)", part)
        if flag:
            tags[flag.group(1)] = int(flag.group(2))
        elif "=" in part:
            key, value = part.split("=", 1)
            tags[key.strip()] = value.strip()
        else:
            tags[part] = True
    return title[: match.start()].strip(), tags


def parse_episode(title: str) -> tuple[int, int, bool]:
    """Return ``(season, episode, proper)``; season is 0 when unknown.

    Patterns are tried most-specific first. Bare numbers are deliberately not
    accepted: with no name to anchor on, "1080p" or "H.264" would match.
    """
    match = _EP_SXXEYY.search(title)
    if match:
        return int(match.group(1)), int(match.group(2)), bool(match.group(3))
    match = _EP_NXNN.search(title)
    if match:
        return int(match.group(1)), int(match.group(2)), bool(match.group(3))
    match = _EP_PREFIXED.search(title)
    if match:
        return 0, int(match.group(1)), bool(match.group(2))
    match = _EP_DASH.search(title)
    if match:
        return 0, int(match.group(1)), bool(match.group(2))
    return 0, 0, False


def _normalise_fallback(
    season: int, episode: int, episodes: dict[str, dict]
) -> tuple[int, int]:
    """Map a title-derived identifier onto the site's season/episode numbering.

    Absolute numbers (``- 55``) must resolve to the same pair a mapped torrent
    would get, otherwise the same episode could be grabbed twice. An absolute
    number that the media does not know is dropped rather than guessed.
    """
    if season > 0 or episode <= 0:
        return season, episode
    matches = [
        entry for entry in episodes.values() if int(entry.get("absolute") or 0) == episode
    ]
    if not matches:
        return 0, 0
    best = max(
        matches, key=lambda entry: (int(entry.get("season") or 0), int(entry.get("episode") or 0))
    )
    return int(best.get("season") or 0), int(best.get("episode") or 0)


def release_from_item(
    item: dict, episodes: dict[str, dict], *, private_magnet: bool = True
) -> Release:
    """Build a :class:`Release` from a ``/torrents/search`` result.

    ``episodes`` is the ``media.episodes`` map from the same response, which is
    what makes the season and episode exact instead of a guess from the title.
    """
    title = str(item.get("title") or "")
    clean, _ = split_tags(title)
    resolution = _RESOLUTION.search(clean)

    group = None
    groups = item.get("groups") or []
    if groups:
        group = str(groups[0].get("name") or groups[0].get("display_name") or "") or None

    mapped = [
        episodes[str(episode_id)]
        for episode_id in item.get("media_episode_ids") or []
        if str(episode_id) in episodes
    ]
    absolute = None
    if len(mapped) == 1:
        season = int(mapped[0].get("season") or 0)
        episode = int(mapped[0].get("episode") or 0)
        absolute = int(mapped[0].get("absolute") or 0) or None
    elif mapped:
        # Several episodes means a pack; track it per season rather than per episode.
        season = min(int(entry.get("season") or 0) for entry in mapped)
        episode = 0
    else:
        season, episode, _ = parse_episode(clean)
        if season == 0 and episode > 0:
            absolute = episode
        season, episode = _normalise_fallback(season, episode, episodes)

    magnet = str(
        (item.get("private_magnet") if private_magnet else item.get("magnet"))
        or item.get("magnet")
        or ""
    )
    infohash = str(item.get("infohash") or "").lower()

    return Release(
        title=title,
        magnet=magnet,
        infohash=infohash,
        size=_as_int(item.get("filesize")),
        level=item.get("level") if isinstance(item.get("level"), int) else None,
        resolution=resolution.group(1).lower() if resolution else None,
        codec=_CODEC_BY_INDEX.get(item.get("video_codec")),
        video_type=_VIDEO_TYPE_BY_INDEX.get(item.get("video_type")),
        group=group,
        season=season,
        episode=episode,
        absolute=absolute,
        batch=bool(item.get("batch")),
    )


# --- http ------------------------------------------------------------------


class NekobtError(RuntimeError):
    """Raised when nekoBT cannot be queried."""


def http_get(
    url: str, *, timeout: float = 30.0, attempts: int = 3, headers: dict | None = None
) -> bytes:
    """GET a URL, honouring 429 Retry-After, with bounded retries."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                wait = _retry_after(exc.headers.get("Retry-After"), attempt)
                LOG.warning("rate limited by nekoBT, waiting %ss", wait)
                time.sleep(wait)
                continue
            if exc.code >= 500 and attempt < attempts:
                time.sleep(attempt)
                continue
            raise NekobtError(f"HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
                continue
            raise NekobtError(f"cannot reach nekoBT: {exc.reason}") from exc
    raise NekobtError(f"giving up on {url}: {last_error}")


def _retry_after(value: str | None, attempt: int) -> int:
    try:
        seconds = int(value or "")
    except (TypeError, ValueError):
        seconds = 5 * attempt
    return max(1, min(seconds, 120))


def fetch_releases(
    apikey: str,
    media_id: str,
    *,
    limit: int = 100,
    levels: list[int] | None = None,
    codecs: list[str] | None = None,
    video_types: list[str] | None = None,
    allow_batch: bool = False,
    private_magnet: bool = True,
) -> list[Release]:
    """Fetch releases for ``media_id`` from nekoBT's search API.

    The response carries each torrent's episode ids plus the media's episode
    map, so season and episode come from the site rather than from the title.
    """
    params = {"media_id": media_id, "sort_by": "latest", "limit": str(limit)}
    if not allow_batch:
        params["batch"] = "false"
    if levels:
        params["levels"] = ",".join(str(level) for level in levels)
    codec_ids = _codec_ids(codecs or [])
    if codec_ids:
        params["video_codec"] = ",".join(str(index) for index in codec_ids)
    type_ids = _video_type_ids(video_types or [])
    if type_ids:
        params["video_type"] = ",".join(str(index) for index in type_ids)

    url = f"{JSON_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    payload = http_get(url, headers={"Cookie": f"ssid={apikey}"})
    try:
        answer = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise NekobtError(f"unparseable search response for {media_id}: {exc}") from exc
    if answer.get("error"):
        raise NekobtError(str(answer.get("message") or "nekoBT search failed"))

    block = answer.get("data") or {}
    media = block.get("media") or {}
    episodes = {
        str(entry.get("id")): entry
        for entry in media.get("episodes") or []
        if entry.get("id") is not None
    }
    return [
        release_from_item(item, episodes, private_magnet=private_magnet)
        for item in block.get("results") or []
    ]


def _video_type_ids(video_types: list[str]) -> list[int]:
    mapped = [_VIDEO_TYPES[name] for name in video_types if name in _VIDEO_TYPES]
    return mapped if len(mapped) == len(video_types) else []


def _codec_ids(codecs: list[str]) -> list[int]:
    """API ids for every requested codec, or none if one of them has no id."""
    mapped = [_CODEC_IDS[codec] for codec in codecs if codec in _CODEC_IDS]
    return mapped if len(mapped) == len(codecs) else []


def lookup_media(query: str, *, limit: int = 5) -> list[dict]:
    """Resolve a title to nekoBT media ids."""
    url = f"{MEDIA_SEARCH_URL}?" + urllib.parse.urlencode({"query": query, "limit": str(limit)})
    payload = http_get(url)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise NekobtError(f"unparseable media search: {exc}") from exc
    if data.get("error"):
        raise NekobtError(data.get("message", "media search failed"))
    return data.get("data", {}).get("results", [])


def _text(item: ET.Element, tag: str) -> str:
    found = item.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _enclosure_url(item: ET.Element) -> str:
    for enclosure in item.findall("enclosure"):
        if "bittorrent" in (enclosure.get("type") or ""):
            return enclosure.get("url") or ""
    enclosure = item.find("enclosure")
    return (enclosure.get("url") or "") if enclosure is not None else ""


def _torznab_attrs(item: ET.Element) -> dict[str, str]:
    return {
        attr.get("name", ""): attr.get("value", "")
        for attr in item.findall("torznab:attr", TORZNAB_NS)
    }


def _as_int(value: str | None) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- configuration ---------------------------------------------------------


class ConfigError(RuntimeError):
    """Raised when config.toml is unusable."""


@dataclass(slots=True)
class Defaults:
    levels: list[int] = field(default_factory=list)
    codecs: list[str] = field(default_factory=list)
    video_types: list[str] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=lambda: ["1080p"])
    allow_batch: bool = False
    private_magnet: bool = True
    begin: tuple[int, int] | None = None
    groups: list[str] = field(default_factory=list)

    def merged(self, overrides: dict) -> Defaults:
        data = {
            "levels": list(self.levels),
            "codecs": list(self.codecs),
            "video_types": list(self.video_types),
            "resolutions": list(self.resolutions),
            "allow_batch": self.allow_batch,
            "private_magnet": self.private_magnet,
            "begin": self.begin,
            "groups": list(self.groups),
        }
        for key, value in overrides.items():
            if value is not None:
                data[key] = value
        return Defaults(**data)


@dataclass(slots=True)
class Show:
    media_id: str
    name: str
    levels: list[int] | None = None
    codecs: list[str] | None = None
    video_types: list[str] | None = None
    resolutions: list[str] | None = None
    groups: list[str] | None = None
    allow_batch: bool | None = None
    private_magnet: bool | None = None
    begin: tuple[int, int] | None = None

    def overrides(self) -> dict:
        return {
            "levels": None if self.levels is None else list(self.levels),
            "codecs": None if self.codecs is None else list(self.codecs),
            "video_types": None if self.video_types is None else list(self.video_types),
            "resolutions": None if self.resolutions is None else list(self.resolutions),
            "allow_batch": self.allow_batch,
            "private_magnet": self.private_magnet,
            "begin": self.begin,
            "groups": None if self.groups is None else list(self.groups),
        }


DEFAULT_CLIENT_TYPE = "transmission"
DEFAULT_CLIENT_URL = "http://127.0.0.1:9091/transmission/rpc"
SECRET_SERVICE = "nekopull"
SECRET_APIKEY = "apikey"
APIKEY_STUB = "libsecret"


@dataclass(slots=True)
class ClientConfig:
    """Connection settings handed to a torrent client backend."""

    type: str = DEFAULT_CLIENT_TYPE
    url: str = DEFAULT_CLIENT_URL
    username: str = ""
    password: str = ""
    download_dir: str | None = None


@dataclass(slots=True)
class Config:
    apikey: str
    apikey_source: str
    defaults: Defaults
    shows: list[Show]
    client: ClientConfig = field(default_factory=ClientConfig)
    state_path: Path = STATE_PATH


_TOP_LEVEL_KEYS = {"apikey", "state_path", "defaults", "show", "client"}
_SHOW_KEYS = {
    "media_id",
    "name",
    "begin",
    "levels",
    "codec",
    "type",
    "resolutions",
    "groups",
    "batch",
    "private_magnet",
}
_DEFAULT_KEYS = {
    "begin",
    "levels",
    "codec",
    "type",
    "resolutions",
    "batch",
    "private_magnet",
    "groups",
}

# config-facing key -> Defaults attribute
_DEFAULTS_ALIASES = {"type": "video_types", "batch": "allow_batch"}
_CLIENT_KEYS = {"type", "url", "username", "password", "download_dir"}
_CODECS = set(_CODEC_IDS)


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


_BEGIN_SEASON = re.compile(r"(?i)^\s*S(\d{1,2})(?:E(\d{1,3}))?\s*$")
_BEGIN_CROSS = re.compile(r"(?i)^\s*(\d{1,2})x(\d{1,3})\s*$")
_BEGIN_ABSOLUTE = re.compile(r"^\s*(\d{1,4})\s*$")


def _begin_value(path: Path, where: str, value) -> tuple[int, int] | int | None:
    """Parse ``begin``: ``S03``/``S03E07``/``3x07`` as a pair, or an absolute number."""
    if value is None:
        return None
    text = str(value).strip()
    match = _BEGIN_SEASON.match(text) or _BEGIN_CROSS.match(text)
    if match:
        return int(match.group(1)), int(match.group(2) or 0)
    match = _BEGIN_ABSOLUTE.match(text)
    if match and not isinstance(value, float):
        return int(match.group(1))
    raise ConfigError(
        f"{path}: {where} `begin` must be S03, S03E07, 3x07, or an absolute episode number"
    )


def _video_type_list(path: Path, where: str, value) -> list[str] | None:
    if value is None:
        return None
    requested = [_slug(item) for item in _as_list(value)]
    by_slug = {_slug(name): name for name in _VIDEO_TYPES}
    unknown = [item for item in requested if item not in by_slug]
    if unknown:
        raise ConfigError(
            f"{path}: {where} has unknown video type(s): {', '.join(unknown)}"
            f" (known: {', '.join(sorted(_VIDEO_TYPES))})"
        )
    return [by_slug[item] for item in requested]


def _codec_list(path: Path, where: str, value) -> list[str] | None:
    if value is None:
        return None
    codecs = [str(item).lower() for item in _as_list(value)]
    unknown = [codec for codec in codecs if codec not in _CODECS]
    if unknown:
        raise ConfigError(
            f"{path}: {where} has unknown codec(s): {', '.join(unknown)}"
            f" (known: {', '.join(sorted(_CODECS))})"
        )
    return codecs


def load_config(path: Path | str = CONFIG_PATH, *, migrate: bool = True) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    unknown_top = set(raw) - _TOP_LEVEL_KEYS
    if unknown_top:
        raise ConfigError(
            f"{path}: unknown keys: {', '.join(sorted(unknown_top))}"
            f" (known: {', '.join(sorted(_TOP_LEVEL_KEYS))})"
        )

    stash = migrate and secret_tool_available()
    stubbed: list[str] = []
    apikey = _credential(
        path, raw, "apikey", "apikey", stash=stash, stubbed=stubbed, required=True
    )
    if str(raw.get("apikey")) == APIKEY_STUB:
        apikey_source = f"libsecret ({SECRET_SERVICE}/{SECRET_APIKEY})"
    elif "apikey" in stubbed:
        apikey_source = "libsecret (migrated)"
    else:
        apikey_source = "config file"

    defaults = Defaults()
    for key, value in raw.get("defaults", {}).items():
        if key not in _DEFAULT_KEYS:
            raise ConfigError(f"{path}: unknown defaults key `{key}`")
        if key == "codec":
            defaults.codecs = _codec_list(path, "[defaults]", value) or []
        elif key == "begin":
            defaults.begin = _begin_value(path, "[defaults]", value)
        elif key == "type":
            defaults.video_types = _video_type_list(path, "[defaults]", value) or []
        elif key == "batch":
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: [defaults] `batch` must be true or false")
            defaults.allow_batch = value
        elif key == "private_magnet":
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: [defaults] `private_magnet` must be true or false")
            defaults.private_magnet = value
        else:
            setattr(defaults, key, value)

    shows: list[Show] = []
    for index, entry in enumerate(raw.get("show", []), start=1):
        unknown = set(entry) - _SHOW_KEYS
        if unknown:
            raise ConfigError(
                f"{path}: show #{index} has unknown keys: {', '.join(sorted(unknown))}"
            )
        media_id = entry.get("media_id")
        if not media_id:
            raise ConfigError(f"{path}: show #{index} is missing `media_id`")
        for boolean_key in ("batch", "private_magnet"):
            if boolean_key in entry and not isinstance(entry[boolean_key], bool):
                raise ConfigError(
                    f"{path}: show #{index}: `{boolean_key}` must be true or false"
                )
        shows.append(
            Show(
                media_id=str(media_id),
                name=str(entry.get("name", media_id)),
                levels=entry.get("levels"),
                codecs=_codec_list(path, f"show #{index}", entry.get("codec")),
                video_types=_video_type_list(path, f"show #{index}", entry.get("type")),
                resolutions=entry.get("resolutions"),
                groups=entry.get("groups"),
                allow_batch=entry.get("batch"),
                private_magnet=entry.get("private_magnet"),
                begin=_begin_value(path, f"show #{index}", entry.get("begin")),
            )
        )
    if not shows:
        raise ConfigError(f"{path}: no [[show]] entries")

    client_raw = raw.get("client", {})
    unknown = set(client_raw) - _CLIENT_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown client keys: {', '.join(sorted(unknown))}")
    client_type = client_raw.get("type", DEFAULT_CLIENT_TYPE)
    if client_type not in CLIENTS:
        raise ConfigError(
            f"{path}: unsupported client `{client_type}` (known: {', '.join(sorted(CLIENTS))})"
        )
    client = ClientConfig(
        type=client_type,
        url=client_raw.get("url", DEFAULT_CLIENT_URL),
        username=_credential(
            path, client_raw, "username", "client-username", stash=stash, stubbed=stubbed
        ),
        password=_credential(
            path, client_raw, "password", "client-password", stash=stash, stubbed=stubbed
        ),
        download_dir=client_raw.get("download_dir"),
    )
    if stubbed and not _stub_config(path, stubbed):
        raise ConfigError(
            f"{path}: stored credentials in libsecret but could not rewrite the file;"
            " replace them with the stub by hand"
        )

    state_path = Path(raw.get("state_path", path.parent / "state.db"))
    return Config(apikey, apikey_source, defaults, shows, client, state_path)


def _credential(
    path: Path,
    table: dict,
    key: str,
    name: str,
    *,
    stash: bool,
    stubbed: list[str],
    required: bool = False,
) -> str:
    value = table.get(key)
    if value is None or value == "":
        if required:
            raise ConfigError(f"{path}: `{key}` is required")
        return ""
    value = str(value)
    if value == APIKEY_STUB:
        stored = read_secret(name)
        if stored is not None:
            return stored
        raise ConfigError(
            f"{path}: no {name} in libsecret. Put it back as {key} = \"...\""
            " and nekopull will store it for you."
        )
    if stash and store_secret(name, value):
        stubbed.append(key)
    return value


def secret_tool_available() -> bool:
    return shutil.which("secret-tool") is not None


def _secret_command(*args: str) -> list[str]:
    return ["secret-tool", *args, "service", SECRET_SERVICE, "username"]


def read_secret(name: str) -> str | None:
    """Return the stored credential, or None when it is not there."""
    if not secret_tool_available():
        return None
    try:
        result = subprocess.run(
            [*_secret_command("lookup"), name],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("cannot query libsecret: %s", exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def store_secret(name: str, value: str) -> bool:
    """Write a credential to libsecret; True when it was stored."""
    if not secret_tool_available():
        return False
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                f"--label=nekopull {name}",
                "service",
                SECRET_SERVICE,
                "username",
                name,
            ],
            input=value + "\n",
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("cannot store %s in libsecret: %s", name, exc)
        return False
    if result.returncode != 0:
        LOG.warning("libsecret refused to store %s: %s", name, result.stderr.strip())
        return False
    return True


def _stub_config(path: Path, keys: list[str]) -> bool:
    """Replace migrated credential values with the stub, in one write."""
    try:
        text = path.read_text()
    except OSError as exc:
        LOG.warning("cannot read %s to move credentials: %s", path, exc)
        return False
    for key in keys:
        pattern = re.compile(
            rf'^(?P<lead>\s*{re.escape(key)}\s*=\s*)(?P<quote>["\']).*?(?P=quote)',
            re.MULTILINE,
        )
        text, count = pattern.subn(lambda match: f'{match.group("lead")}"{APIKEY_STUB}"', text, count=1)
        if not count:
            LOG.warning("cannot find `%s =` in %s", key, path)
            return False
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    except OSError as exc:
        LOG.warning("cannot rewrite %s: %s", path, exc)
        temporary.unlink(missing_ok=True)
        return False
    LOG.info("credentials moved to libsecret; %s now uses the stub", path)
    return True


# --- state -----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grabbed (
    infohash  TEXT PRIMARY KEY,
    media_id  TEXT NOT NULL,
    season    INTEGER NOT NULL,
    episode   INTEGER NOT NULL,
    title     TEXT NOT NULL,
    level     INTEGER,
    grabbed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS grabbed_episode ON grabbed (media_id, season, episode);
"""


class State:
    """Remembers which releases and episodes were already handed off."""

    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def has_release(self, infohash: str) -> bool:
        if not infohash:
            return False
        row = self._connection.execute(
            "SELECT 1 FROM grabbed WHERE infohash = ?", (infohash,)
        ).fetchone()
        return row is not None

    def has_episode(self, media_id: str, season: int, episode: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM grabbed WHERE media_id = ? AND season = ? AND episode = ?",
            (media_id, season, episode),
        ).fetchone()
        return row is not None

    def record(self, media_id: str, release: Release) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO grabbed"
            " (infohash, media_id, season, episode, title, level, grabbed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                release.infohash,
                media_id,
                release.season,
                release.episode,
                release.title,
                release.level,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self._connection.commit()


# --- torrent clients -------------------------------------------------------


class ClientError(RuntimeError):
    """Raised when the torrent client cannot be reached or refuses a request."""


class TorrentClient:
    """Backend interface every client implements."""

    name = "base"

    def __init__(self, config: ClientConfig, *, timeout: float = 30.0):
        self.url = config.url
        self.download_dir = config.download_dir
        self.timeout = timeout

    def version(self) -> str:
        raise NotImplementedError

    def add(self, magnet: str, tags: list[str] | None = None) -> str:
        """Add a magnet link; return ``added``, ``duplicate`` or ``error``."""
        raise NotImplementedError


class Transmission(TorrentClient):
    """Transmission RPC backend (session id handshake included)."""

    name = "transmission"

    def __init__(self, config: ClientConfig, *, timeout: float = 30.0):
        super().__init__(config, timeout=timeout)
        self.session_id: str | None = None
        self._authorization = ""
        if config.username:
            token = base64.b64encode(f"{config.username}:{config.password}".encode()).decode()
            self._authorization = f"Basic {token}"

    def _call(self, method: str, arguments: dict | None = None) -> dict:
        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        for _ in range(2):
            headers = {
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self._authorization:
                headers["Authorization"] = self._authorization
            if self.session_id:
                headers["X-Transmission-Session-Id"] = self.session_id
            request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    self.session_id = exc.headers.get("X-Transmission-Session-Id")
                    exc.close()
                    if not self.session_id:
                        raise ClientError("Transmission did not send a session id") from exc
                    continue
                raise ClientError(f"Transmission HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise ClientError(f"cannot reach Transmission: {exc.reason}") from exc
            try:
                answer = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ClientError(f"unparseable Transmission response: {exc}") from exc
            if answer.get("result") != "success":
                raise ClientError(f"Transmission refused {method}: {answer.get('result')}")
            return answer.get("arguments", {})
        raise ClientError("Transmission session handshake failed")

    def version(self) -> str:
        return self._call("session-get").get("version", "unknown")

    def add(self, magnet: str, tags: list[str] | None = None) -> str:
        arguments: dict = {"filename": magnet}
        if self.download_dir:
            arguments["download-dir"] = self.download_dir
        if tags:
            arguments["labels"] = tags
        answer = self._call("torrent-add", arguments)
        if "torrent-added" in answer:
            return "added"
        if "torrent-duplicate" in answer:
            return "duplicate"
        return "error"


CLIENTS: dict[str, type[TorrentClient]] = {Transmission.name: Transmission}


def make_client(config: ClientConfig) -> TorrentClient:
    backend = CLIENTS.get(config.type)
    if backend is None:
        raise ConfigError(
            f"unsupported client `{config.type}` (known: {', '.join(sorted(CLIENTS))})"
        )
    return backend(config)


# --- selection -------------------------------------------------------------


def passes_filters(release: Release, settings: Defaults) -> bool:
    if not settings.allow_batch and release.batch:
        return False
    if settings.resolutions and release.resolution not in settings.resolutions:
        return False
    if settings.begin is not None:
        if isinstance(settings.begin, tuple):
            if release.episode_key < settings.begin:
                return False
        elif release.absolute is not None and release.absolute < settings.begin:
            return False
    if settings.levels and release.level not in settings.levels:
        return False
    if settings.codecs and release.codec not in settings.codecs:
        return False
    if settings.video_types and release.video_type not in settings.video_types:
        return False
    return True


def rank(release: Release, settings: Defaults) -> tuple:
    """Sort key: preferred group, then highest sub level."""
    preferred = {group.lower() for group in settings.groups}
    group_rank = 0 if not preferred else (0 if (release.group or "").lower() in preferred else 1)
    level = release.level if release.level is not None else -1
    return (group_rank, -level)


def select(
    releases: list[Release],
    settings: Defaults,
    *,
    media_id: str = "",
    state: State | None = None,
) -> list[Release]:
    """Pick at most one release per episode, best-first, skipping known ones."""
    candidates: dict[tuple[int, int], list[Release]] = {}
    for release in releases:
        if not release.infohash:
            continue
        if release.episode <= 0 and not (release.batch and settings.allow_batch):
            continue
        if not passes_filters(release, settings):
            continue
        candidates.setdefault(release.episode_key, []).append(release)

    chosen: list[Release] = []
    for key in sorted(candidates):
        if state is not None and media_id and state.has_episode(media_id, key[0], key[1]):
            continue
        best = min(candidates[key], key=lambda release: rank(release, settings))
        if state is not None and state.has_release(best.infohash):
            continue
        chosen.append(best)
    return chosen


# --- commands --------------------------------------------------------------


def command_run(config: Config, options: argparse.Namespace) -> int:
    selected = _select_shows(config, options.show)
    failures = 0
    added = 0
    with State(config.state_path) as state:
        client = None if options.dry_run else make_client(config.client)
        for show in selected:
            settings = config.defaults.merged(show.overrides())
            try:
                releases = fetch_releases(
                    config.apikey,
                    show.media_id,
                    levels=settings.levels,
                    codecs=settings.codecs,
                    video_types=settings.video_types,
                    allow_batch=settings.allow_batch,
                    private_magnet=settings.private_magnet,
                )
            except NekobtError as exc:
                LOG.error("%s: %s", show.name, exc)
                failures += 1
                continue
            picks = select(releases, settings, media_id=show.media_id, state=state)
            LOG.info("%s: %d releases, %d new episode(s)", show.name, len(releases), len(picks))
            for release in picks:
                description = (
                    f"{show.name} S{release.season:02d}E{release.episode:02d}"
                    f" [{release.resolution or '?'}"
                    f"{', L' + str(release.level) if release.level is not None else ''}]"
                    f" {release.title[:70]}"
                )
                if options.dry_run:
                    LOG.info("[dry-run] would add %s", description)
                    added += 1
                    continue
                assert client is not None
                try:
                    result = client.add(release.magnet, tags=[show.name])
                except ClientError as exc:
                    LOG.error("%s: %s", show.name, exc)
                    failures += 1
                    break
                LOG.info("%s (%s)", description, result)
                if result == "added":
                    added += 1
                state.record(show.media_id, release)
    LOG.info("done: %d added, %d failure(s)", added, failures)
    return 1 if failures else 0


def _select_shows(config: Config, wanted: list[str] | None) -> list[Show]:
    if not wanted:
        return config.shows
    lookup = {show.name.lower(): show for show in config.shows}
    lookup.update({show.media_id.lower(): show for show in config.shows})
    chosen = []
    for name in wanted:
        show = lookup.get(name.lower())
        if show is None:
            raise ConfigError(f"unknown show: {name}")
        chosen.append(show)
    return chosen


def _format_value(value) -> str:
    if isinstance(value, tuple) and len(value) == 2:
        return f"S{value[0]:02d}E{value[1]:02d}"
    return str(value)


def command_lookup(options: argparse.Namespace) -> int:
    results = lookup_media(options.query, limit=options.limit)
    if not results:
        print(f"no media matched {options.query!r}")
        return 1
    for result in results:
        print(
            f"{result.get('id', '?'):>8}  {result.get('year', '????')}  {result.get('title', '')}"
        )
    return 0


def command_list(config: Config) -> int:
    for show in config.shows:
        display = {"video_types": "type", "allow_batch": "batch"}
        overrides = {
            display.get(key, key): value
            for key, value in show.overrides().items()
            if value is not None
        }
        extra = " ".join(f"{key}={_format_value(value)}" for key, value in overrides.items())
        print(f"{show.media_id:>10}  {show.name}" + (f"  ({extra})" if extra else ""))
    return 0


def command_verify(config: Config) -> int:
    failures = 0
    print(f"api key: {config.apikey_source}")
    try:
        releases = fetch_releases(
            config.apikey,
            config.shows[0].media_id,
            limit=1,
            private_magnet=config.defaults.private_magnet,
        )
    except NekobtError as exc:
        print(f"nekoBT: {exc}", file=sys.stderr)
        failures += 1
    else:
        print(f"api key: works ({len(releases)} release(s) for {config.shows[0].name})")
    try:
        version = make_client(config.client).version()
    except ClientError as exc:
        print(f"{config.client.type}: {exc}", file=sys.stderr)
        failures += 1
    else:
        print(f"{config.client.type}: OK (version {version})")
    print(f"shows: {len(config.shows)}")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nekopull", description="Watch nekoBT for new episodes of the shows you follow."
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.toml")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="poll every configured show")
    run.add_argument("--dry-run", action="store_true", help="report without adding anything")
    run.add_argument("--show", action="append", metavar="NAME", help="only this show (repeatable)")
    run.set_defaults(func=command_run)

    lookup = subparsers.add_parser("lookup", help="resolve a title to show ids")
    lookup.add_argument("query")
    lookup.add_argument("--limit", type=int, default=5)
    lookup.set_defaults(func=lambda config, options: command_lookup(options))

    listing = subparsers.add_parser("list", help="print configured shows")
    listing.set_defaults(func=lambda config, options: command_list(config))

    verify = subparsers.add_parser("verify", help="check the API key, nekoBT and the client")
    verify.set_defaults(func=lambda config, options: command_verify(config))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if options.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    migrate = options.command in {"run", "verify"} and not getattr(options, "dry_run", False)
    try:
        config = load_config(options.config, migrate=migrate)
    except ConfigError as exc:
        if options.command == "lookup":
            config = None  # lookup talks to nekoBT's public search only
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        return options.func(config, options)
    except (ConfigError, NekobtError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
