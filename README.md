# nekopull

A small poller for nekoBT. It checks the shows you list for
new episodes and hands the magnet link to your torrent client — Transmission
today, others by adding a backend.

## Dependencies

Python 3.11+

## Install

`PKGBUILD` on Arch, or:

```sh
python3 -m venv .venv && .venv/bin/pip install .
```

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

4. Run it daily:

   ```sh
   systemctl --user enable --now nekopull.timer
   ```

   `nekopull.service` is a oneshot unit and `nekopull.timer` fires it at 04:00
   every day, with `Persistent=true` so a run missed while the machine was off
   happens at the next boot.

   For another time, override the packaged unit instead of editing it:

   ```sh
   systemctl --user edit nekopull.timer
   ```

   ```ini
   [Timer]
   OnCalendar=
   OnCalendar=*-*-* 06:00:00
   ```

   The empty assignment resets the packaged schedule; without it both would
   fire. `systemctl edit` reloads configuration itself, so the running timer
   picks up the new value.

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
| `levels` | any | sub levels to accept, e.g. `[2, 3]` |
| `resolutions` | `["1080p"]` | accepted resolutions; a release without one is skipped |
| `groups` | `[]` | release groups to prefer (a preference, not a filter) |
| `min_seeders` | `0` | skip releases with fewer seeders |
| `allow_batch` | `false` | accept season batches |

One release is taken per episode. When several match, the order of preference is
your `groups`, then the highest accepted sub level, then the most seeders.

**Sub levels** describe how much work went into the subtitles:

| level | meaning |
| --- | --- |
| 0 | official subtitles, untouched |
| 1 | official subtitles, lightly edited |
| 2 | small-scale fansubs |
| 3 | full-scale fansubs |

Leave `levels` unset to accept any. Only listed levels are eligible, and an
untagged release is skipped when the list is set.

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
