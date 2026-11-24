# nekopull

Poll [nekoBT](https://nekobt.to) **by media id** and hand new episodes to Transmission.

nekoBT's Torznab endpoint accepts `media_id`, which scopes a feed to exactly one
show. nekopull uses that as the only source of truth: it never decides *which
show* a release belongs to, never compares title spellings, and never needs an
alternate name when a release is titled in English and your list is in romaji.
Titles are parsed only for the episode number, resolution, codec and nekoBT's
`{Tags:L0;V9;...}` block — all properties of a single release.

## Why not an indexer client

Sonarr/Prowlarr key on TVDB/TMDB ids and do not solve the title problem, but
their Torznab client can only send `q`, `cat` and the database ids. nekoBT's
useful filters — `media_id`, `levels`, `batch`, `group_id` — have no
representation there. flexget can send them, but its series engine is
title-first: it buckets entries by the first letter of the title it guesses and
needs a name per show.

nekopull keeps every nekoBT filter and needs no names at all.

## Install

Arch: `nekopull` from the AUR (see `PKGBUILD` here), or:

```sh
python3 -m venv .venv && .venv/bin/pip install .
```

Python 3.11+, no runtime dependencies.

## Configure

```sh
mkdir -p ~/.config/nekopull
cp config.example.toml ~/.config/nekopull/config.toml
$EDITOR ~/.config/nekopull/config.toml
```

Look up ids with your own API key:

```sh
nekopull lookup "towa no yuugure"
#     s1539  2025  Dusk Beyond the End of the World
```

Then:

```sh
nekopull verify        # Transmission reachable, config sane
nekopull list          # configured shows and their overrides
nekopull run --dry-run # what would be added, adds nothing
nekopull run           # hand magnets to Transmission
```

Exit status is `0` on success, `1` when a show or Transmission failed, `2` for
config problems.

## Filters

Per-show values override `[defaults]`:

| key | meaning |
| --- | --- |
| `max_level` | highest nekoBT sub level to accept (`0` = official only, `null` = any) |
| `resolutions` | accepted resolutions; a release without one is rejected |
| `min_seeders` | skip releases with fewer seeders |
| `allow_batch` | accept season batches |
| `allow_v2` | accept `v2`-style re-releases |
| `groups` | preferred groups, ranked first (not exclusive) |

One release per episode is chosen: preferred group, then lowest sub level, then
most seeders. Grabbed releases and episodes are recorded in
`~/.local/state/nekopull/state.db`, so a re-poll never re-adds anything.

## Layout

```
~/.config/nekopull/config.toml    apikey, Transmission, shows
~/.local/state/nekopull/state.db  grabbed releases and episodes
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Licence

MIT.
