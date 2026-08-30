# Design

Obozrenie is a desktop server browser: adapters fetch server lists, core stores them, GTK shows them and launches clients.

## Core

`GameTable` holds per-game `info` (name, adapter, launch pattern, optional Steam app id), `settings`, `servers`, and query status. Callers use accessors; they do not mutate the inner table directly.

`Core` loads `game_lists.toml`, optionally opens a MaxMind country DB (`geoip.find_database()`: system paths, then XDG cache), and runs `stat_master` on a daemon thread per game. After a successful fetch it geolocates each host (`country_code` or nested `country.iso_code`) and writes servers back.

`Settings` merges default TOML with the XDG config profile.

## Adapters

`adapters.adapter_table` maps name → module with `stat_master(game, game_info, master_list) -> list[dict]`.

- **qstat** — subprocess `qstat -xml`, parse with xmltodict. Master host order is preserved and de-duplicated (`dict.fromkeys`); qstat returns nothing if the last master times out.
- **minetest** — HTTP JSON list, then ICMP RTT via `ping.Pinger`.
- **rigsofrods** — HTTP JSON from the live API (`https://api.rigsofrods.org/server-list`). Request URL gains `?json=true` if absent. Each entry maps `verified` / `is-official` into `rules`, and `json-userlist` into `players`. Launch is the configured binary only (no config-file rewrite).

There is no proxy layer.

## Ping

`Pinger` is instance-scoped (status, host queue, lock). `do_launch` uses `$PWD` when set, otherwise `os.getcwd()`.

## UI

GTK 3 (`gi`). Country is shown as flag emoji from the ISO code. Missing GeoIP offers a cancellable download into cache.

## Data

Source checkout: `<package>/../assets` and `locale`. Installed: `sys.prefix/share` (setuptools `data-files`). User settings: `~/.config/obozrenie`.
