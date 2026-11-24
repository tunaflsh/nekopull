# nekopull

Keeps the anime you follow up to date. nekopull checks nekoBT for new episodes
of the shows you list, picks one release per episode, and hands the magnet link
to your torrent client. Run it from a timer and forget about it.

## Features

- **Your shows, not your search terms.** Each entry is bound to one show on
  nekoBT, so a poll returns that show's releases and nothing else.
- **One release per episode.** By default it takes the most complete subtitles
  (nekoBT's *sub level*), then your preferred group, then the most seeders.
- **No duplicates.** Grabbed releases and episodes are remembered in a small
  SQLite database, so re-running never adds the same episode twice.
- **Filters that matter for anime:** subtitle level, resolution, release group,
  season batches, minimum seeders — globally and per show.
- **`--dry-run`** prints what it would add, without touching your client.

## Install

From the AUR (`nekopull`), or from a checkout:

```sh
python3 -m venv .venv && .venv/bin/pip install .
```

Python 3.11+. No runtime dependencies.

## Set up

1. Find the id of a show:

   ```sh
   nekopull lookup "towa no yuugure"
   #     s1539  2025  Dusk Beyond the End of the World
   ```

2. Create `~/.config/nekopull/config.toml` (start from
   `/usr/share/doc/nekopull/config.example.toml`):

   ```toml
   apikey = "YOUR-API-KEY"

   [client]
   type = "transmission"
   url = "http://127.0.0.1:9091/transmission/rpc"

   [[show]]
   media_id = "s1539"
   name = "towa no yuugure"
   ```

3. Check it, then commit:

   ```sh
   nekopull verify
   nekopull run --dry-run
   nekopull run
   ```

4. Run it hourly:

   ```sh
   systemctl --user enable --now nekopull.timer
   ```

   `nekopull.service` is a oneshot unit; `nekopull.timer` runs it once an hour
   with a random delay. Logs go to the journal: `journalctl --user -u nekopull`.

## Configuration

Everything except `[[show]]` is optional.

| key | default | meaning |
| --- | --- | --- |
| `apikey` | — | nekoBT API key; may be replaced by `[secrets]` |
| `state_path` | `~/.local/state/nekopull/state.db` | where grabbed episodes are recorded |

### `[defaults]` and per-show overrides

Any key here can also be set on a single `[[show]]` to override it.

| key | default | meaning |
| --- | --- | --- |
| `levels` | any | sub levels to accept, e.g. `[2, 3]`; see below |
| `resolutions` | `["1080p"]` | accepted resolutions; a release without one is skipped |
| `groups` | `[]` | release groups to prefer (a preference, not a filter) |
| `min_seeders` | `0` | skip releases with fewer seeders |
| `allow_batch` | `false` | accept season batches |

**Sub levels** describe how much work went into the subtitles:

| level | meaning |
| --- | --- |
| 0 | official subtitles, untouched |
| 1 | official subtitles, lightly edited |
| 2 | small-scale fansubs |
| 3 | full-scale fansubs |

Higher is more complete, so when several levels are accepted the highest one
wins. Leave `levels` unset to accept any. Only levels in the list are eligible —
an untagged release is skipped when the list is set.

### `[client]`

| key | meaning |
| --- | --- |
| `type` | client backend; currently `transmission` |
| `url` | RPC endpoint |
| `username`, `password` | optional HTTP auth |
| `download_dir` | optional download location passed to the client |

To support another client, implement `version()` and `add(magnet, tags)` in a
`TorrentClient` subclass and register it in `CLIENTS`; nothing else changes.

### Storing the API key in libsecret

```sh
secret-tool store --label='nekopull API key' service nekopull username api
```

```toml
[secrets]
provider = "libsecret"
service = "nekopull"   # default
username = "api"       # default
```

If `apikey` is present it wins; otherwise the key is read from the Secret
Service at start-up, so it never needs to be in the config file.

## Commands

| command | what it does |
| --- | --- |
| `nekopull run` | poll every show, add new episodes |
| `nekopull run --dry-run` | report only, add nothing |
| `nekopull run --show NAME` | restrict to one show (repeatable) |
| `nekopull lookup TEXT` | find show ids |
| `nekopull list` | show configuration and overrides |
| `nekopull verify` | check the API key, nekoBT and the client |

Exit status: `0` success, `1` a show or the client failed, `2` configuration
problem.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Licence

MIT.
