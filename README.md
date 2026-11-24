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

   To use a different time, add a drop-in:

   ```sh
   systemctl --user edit nekopull.timer
   ```

   ```ini
   [Timer]
   OnCalendar=
   OnCalendar=*-*-* 06:00:00
   ```

   The empty assignment clears the packaged schedule; without it both would
   fire.

## Configuration

Everything except `[[show]]` is optional.

| key | default | meaning |
| --- | --- | --- |
| `apikey` | — | nekoBT API key; moved to libsecret automatically when available |
| `state_path` | `~/.local/state/nekopull/state.db` | where grabbed episodes are recorded |

### `[defaults]` and per-show overrides

Any key here can also be set on a single `[[show]]` to override it.

| key | default | meaning |
| --- | --- | --- |
| `levels` | any | sub levels to accept, e.g. `[2, 3]` |
| `codec` | any | accepted codecs: `h264`, `h265`, `av1`, `vp9`, `mpeg2`, `xvid`, `wmv`, `vc1`; a string or a list |
| `type` | any | release type, e.g. `web_dl`, `bd_remux`, `dvd_raw`; a string or a list |
| `resolutions` | `["1080p"]` | accepted resolutions, read from the title; a release without one is skipped |
| `groups` | `[]` | release groups to prefer (a preference, not a filter) |
| `batch` | `false` | accept season batches, tracked per season |
| `private_magnet` | `true` | hand over your private magnet (carries your passkey) |

One release is taken per episode. When several match, the order of preference is
your `groups`, then the highest accepted sub level; then the newest release in the feed.

`begin` is not a default: it belongs to one show, since it says how far you are
into that series. Set it on a `[[show]]` as `S03`, `S03E07`, `3x07` or an
absolute number. It compares against the episode the site resolved, so it works
for shows numbered absolutely; an absolute `begin` can only filter releases
whose episode nekoBT knows — a torrent with no Media Episodes set is never
skipped.

**Sub levels** describe how much work went into the subtitles:

| level | meaning |
| --- | --- |
| 0 | official subtitles, untouched |
| 1 | official subtitles, lightly edited |
| 2 | small-scale fansubs |
| 3 | full-scale fansubs |

Leave `levels` unset to accept any. Only listed levels are eligible, and an
untagged release is skipped when the list is set.

**`type`** is where the release came from rather than what is in it:
`web_dl`, `web_encode`, `web_mini`, `bd_disc`, `bd_mini`, `bd_encode`,
`bd_remux`, `dvd_disc`, `dvd_raw`, `dvd_encode`, `tv_raw`, `tv_encode`, `vhs`,
`laserdisc`, `hybrid`, `other`.

`levels`, `codec` and `type` are also sent to nekoBT, so the newest-100
window contains matching releases instead of whatever was uploaded last. A
`codec` or `type` list containing a value the API does not know stays a local
filter.

### `[client]`

| key | meaning |
| --- | --- |
| `type` | client backend; currently `transmission` |
| `url` | RPC endpoint |
| `username`, `password` | optional HTTP auth |
| `download_dir` | optional download location passed to the client |

To support another client, implement `version()` and `add(magnet, tags)` in a
`TorrentClient` subclass and register it in `CLIENTS`; nothing else changes.

### Where credentials live

If `secret-tool` (libsecret) is installed, the first `run` or `verify` moves
every credential it finds into the Secret Service and leaves the stub behind:

```toml
apikey = "libsecret"

[client]
username = "libsecret"
password = "libsecret"
```

Entries are stored as `service nekopull username <name>`, where `<name>` is
`apikey`, `client-username` or `client-password`. From then on the values are
read back at start-up, so `config.toml` holds no secrets. Without libsecret the
values stay in the file and are used as is; `--dry-run` never rewrites anything.

If the keyring is reset, nekopull names the missing entry — put a real value
back in the file and it will be stored again.

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
