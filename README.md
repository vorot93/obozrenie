![Obozrenie](https://raw.githubusercontent.com/vorot93/obozrenie/master/obozrenie/data/icons/hicolor/scalable/apps/obozrenie.svg)

Simple and easy to use game server browser for Linux and macOS.

## Overview
Obozrenie lists multiplayer game servers and can launch the game client to connect.

## Features
- Threaded master-server queries
- Bounded wall-clock waits for server-list HTTP and RTT/GeoIP enrichment
- Transient connection passwords that are never saved in user settings
- Keyboard-focusable selectors, filter mnemonics, and a searchable game list
- GTK 4 and libadwaita UI
- QStat, Minetest, and Rigs of Rods adapters

## Screenshot
![](https://raw.githubusercontent.com/vorot93/obozrenie/master/screenshot.png)

## Dependencies
### Required
- [Python 3.11+](https://python.org)
- [PyGObject](https://pygobject.gnome.org) with GTK 4.14+ and [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) 1.5+ (built against the native GTK stack)
- [PyXDG](https://www.freedesktop.org/wiki/Software/pyxdg/)
- [tomli-w](https://github.com/hukkin/tomli-w) (TOML writing; reading uses stdlib `tomllib`)
- [xmltodict](https://github.com/martinblech/xmltodict)
- [requests](https://requests.readthedocs.io)
- [urllib3](https://urllib3.readthedocs.io) (byte-granular response reads and
  the cross-thread read shutdown the GeoIP download's bounds rest on)
- [icmplib](https://github.com/ValentinBELYN/icmplib)
- [maxminddb](https://github.com/maxmind/MaxMind-DB-Reader-python)
- [filelock](https://github.com/tox-dev/filelock) (cross-process settings profile lock)

Python packages are declared in `pyproject.toml`.

### Development
- [Babel](https://babel.pocoo.org) for translation catalogs
- [PyYAML](https://pyyaml.org) for source-only metadata and workflow checks

`uv sync` installs the development dependency group by default.

### Optional
- [QStat](https://github.com/multiplay/qstat) — QStat-backed games; the executable may be installed as `qstat` or `quakestat`
- A MaxMind DB country database (`.mmdb`) — server locations as flag emoji. The app probes common system paths (e.g. `/usr/share/GeoIP/`); if none is found it can download a free [DB-IP](https://db-ip.com/) country database, as described under [Server locations](#server-locations). No MaxMind account is required.

## Get it
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) to run
from a checkout. Every installation also needs a native GTK 4 and libadwaita
stack.

### Native prerequisites

Linux (Debian/Ubuntu):

    sudo apt install adwaita-icon-theme gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 gir1.2-gtk-4.0 gcc libadwaita-1-0 libcairo2-dev libgirepository-2.0-dev libgtk-4-1 pkg-config

Linux (Arch):

    sudo pacman -S --needed gtk4 libadwaita gobject-introspection cairo pkgconf

macOS:

    brew install gtk4 libadwaita gobject-introspection adwaita-icon-theme pkg-config cairo

Obozrenie checks the toolkit at startup and refuses to run on GTK older than
4.14 or libadwaita older than 1.5.

### Run from a checkout

    git clone https://github.com/vorot93/obozrenie.git
    cd obozrenie
    uv sync
    uv run obozrenie-gtk

uv compiles PyGObject and pycairo against the system GTK libraries. On macOS the launcher adds Homebrew's library directory to the dynamic loader path.

### Install the application
After installing the native prerequisites, install either the project checkout
or a built wheel, then use the installed console entry point:

    python -m pip install .
    # Or: python -m pip install /path/to/obozrenie-1-py3-none-any.whl
    obozrenie-gtk

The wheel contains the UI definitions, default settings, game icons, and
Russian gettext catalog. Runtime resources are read from the installed package
and do not depend on the checkout, current directory, or installation prefix.

## Server endpoints
Enter an explicit `host:port`, such as `example.org:27960` or
`192.0.2.10:30000`. IPv6 addresses must be bracketed, for example
`[2001:db8::10]:27960`. Host-only input and default-port shorthand are not
supported.

## Refreshing server lists
Minetest, Freeminer, and Rigs of Rods fetch their master lists over HTTPS, and
only over HTTPS: redirects are followed at most five times per request and
only to another HTTPS address, and a redirect that points at plain HTTP is
refused before anything is sent to it. QStat-backed games reach their masters
through the QStat binary instead.

When such a game lists several masters, all of them are queried under one
shared time budget and each one stands alone: a master that is unreachable,
times out, or answers with something unusable is logged and skipped while the
servers the other masters returned are kept, and a master that legitimately
returns an empty list still counts as a success. The refresh fails only when
every configured master fails.

## Launching games
A game's **Game path** setting names the client to start:

- A bare command such as `xonotic` is looked up in `PATH`.
- An absolute path is used as written.
- A relative path that contains a directory, such as `bin/xonotic`, is resolved
  against that game's **Working directory**, or against the directory Obozrenie
  itself was started in when no working directory is set.

When a **Working directory** is set, the client runs there. With none set, the
client runs in the directory of the resolved executable, or — for a bare
command — in the directory Obozrenie itself was started in. **Launch via
Steam** is the exception: it starts the Steam client rather than the game
binary, so it ignores the game's working directory and runs from the directory
of the **Steam path** binary, or from the directory Obozrenie was started in
for a bare `steam` command.

Obozrenie checks the setting that names the executable before it starts
anything: **Game path**, or **Steam path** for a game set to launch via Steam.
If that setting is empty or blank — as one shipped game default's **Game path**
is — Obozrenie answers `Please configure the executable path for <game>.` and
launches nothing.

The connection password is used for that one launch only. It is never written
to your settings, and the launch details Obozrenie displays show it as `***`.

## Server locations
When no country database is found, the GTK frontend offers to download a free
DB-IP country database into the user cache. The offer is a dialog with **Not
now** and **Download**; an accepted download shows its progress in a dialog
whose **Cancel** button, close button, or Escape stops it. Only one offer and
one download exist at a time: while an offer or its transfer is live,
activating Obozrenie only presents the main window again instead of stacking a
second offer or starting a second download. A download gives up 120 seconds
in, however slowly the server sends, refuses a body larger than 128 MiB, and
streams into a private temporary file beside its destination; it puts the
database in place only once the downloaded file opens as a valid MaxMind
database, so nothing unvalidated is ever installed.

Cancelling stops the download and removes the partial file. It never waits for
the transfer: pressing "Not now" and quitting both return immediately, whatever
the download is doing at that moment. A cancellation that arrives after the
validated database has already been put in place leaves it installed, and
Obozrenie loads and uses it — by then there is nothing unvalidated to undo.
Quitting while a download runs stops it the same way, and a database put in
place at the last moment stays on disk for the next start instead of being
loaded into the closing session.

## Settings files
User settings live in `~/.config/obozrenie`, with a lock file
`~/.config/obozrenie.lock` beside that directory. Obozrenie holds that lock
while it loads and while it saves, so two running copies cannot interleave
writes; a copy that cannot take the lock within five seconds reports a
settings error instead of waiting indefinitely, and — like every other failure
to load or save settings — ends the run with a failure exit status rather than
a silent success. The lock file deliberately stays on disk after use — it is
never stale, and deleting it by hand breaks the mutual exclusion it provides.

If a settings file cannot be parsed — malformed TOML or invalid UTF-8 —
Obozrenie copies exactly those bytes into a private sibling named after it,
such as `games.toml.invalid-1a2b3c`, readable only by you; it reports the
problem once at startup and continues with the defaults. The malformed file
itself is never renamed, moved, deleted, or otherwise changed by that
recovery, so a good replacement written by another running copy is never
destroyed. The next successful save replaces it. A settings file that cannot
be read at all — the wrong permissions, for example — is not recovered that
way: Obozrenie reports `Settings file could not be read.` as a startup failure
and stops instead of starting on the defaults. A settings file it cannot write
reports `Settings file could not be written.` the same way when the session
ends, and a malformed file whose private backup cannot be written — the
settings directory is full or not writable — reports `Settings backup could
not be written.` and stops the same way at startup. None of these messages
repeats the file's path or the operating system's own error text.

## Translations
The interface language follows the standard gettext message-locale variables:
the first nonempty of `LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG` wins outright
and `LC_CTYPE` is ignored. A Russian catalog ships with the application, so:

    LANGUAGE=ru_RU obozrenie-gtk

## Translation development (Git checkout only)
These commands are development workflows for a Git checkout. Built wheels and
unpacked source distributions intentionally do not include these scripts or
tools/.

Refresh, compile, and validate the packaged catalogs with:

    ./update_translations.sh
    ./compile_translations.sh
    uv run python tools/check_translations.py

The scripts resolve the checkout from their own location, so they can also be invoked from another working directory.
Stable catalog metadata lives in `[tool.obozrenie.translations]` in
`pyproject.toml`. Update `pot-creation-date` for intentional source-message
changes and `po-revision-date` for intentional Russian translation changes;
ordinary regeneration preserves byte-identical POT, PO, and MO files.
Catalog generation uses the exact dev-only Babel pin in `pyproject.toml`. An
intentional Babel upgrade must change that pin, run both scripts to regenerate
the catalogs, and rerun the checker before accepting the resulting artifacts.

## Technical details
Python + PyGObject (GTK 4, libadwaita). Adapters query masters; core holds the game/server tables and settings; a GTK 4 window shows them through a display-free presenter. Server-list HTTP calls and the RTT and GeoIP enrichment performed during a refresh use bounded batch waits. QStat-backed games discover the optional executable as `qstat` first and `quakestat` second. Runtime data, including the compiled Russian catalog, is loaded from package-resource streams independently of the process working directory and installation prefix. Python and template-document strings use the same gettext catalog object; marked template text is translated in Python before GTK parses the XML.

## Licenses
### Obozrenie
Obozrenie and its original application artwork are licensed under the GNU General Public License version 3 (GPL-3.0-only). See `COPYING` for the full license text.
