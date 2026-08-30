# Working conventions

Python 3.11+ GTK 3 game server browser. `pyproject.toml` is the only dependency list.

## Run

- Linux: GTK 3 + GObject introspection (`gir1.2-gtk-3.0`, `pkg-config`, cairo/girepository headers). Then `uv sync` and `uv run obozrenie-gtk`, or `pip install .` and `./obozrenie-gtk`. uv builds PyGObject into the venv; distro `python3-gi` is only for a system interpreter.
- macOS: `brew install gtk+3 gobject-introspection adwaita-icon-theme pkg-config cairo`, then `uv sync` and `uv run obozrenie-gtk`. The launcher re-execs once to set `DYLD_FALLBACK_LIBRARY_PATH` to Homebrew's lib dir. Bare `import obozrenie.gtk` does **not** go through that path — set the env var yourself (CI does this job-wide).
- Data files: if `<repo>/assets` exists next to the `obozrenie` package, that tree (and `<repo>/locale`) is used, independent of cwd. Otherwise `sys.prefix/share`.

## Test

From the repo root:

    python -m tests.tests

macOS, when the suite imports GTK (`IconLoadingTests`, `CountryEmojiTests`):

    DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib" uv run python -m tests.tests

Linux CI: `pip install .` then `python -m tests.tests` (system GTK). macOS CI: `uv sync` plus the dyld env var, then the same suite and `import obozrenie.gtk`.

QStat is optional; adapter unit tests do not invoke the binary.

## Layout

- `obozrenie/` — core, GTK UI, launch, GeoIP, adapters (`qstat`, `minetest`, `rigsofrods`)
- `assets/` — UI, default settings, game list, icons
- `tests/tests.py` — unittest module

No `docs/superpowers/` (or other process-artifact trees) in this repo.

## Gotchas

- `launch_gtk.main()` is the console-script entry; importing the module must not start GTK.
- Rigs of Rods master URIs get `?json=true` if missing; live API is `https://api.rigsofrods.org/server-list`.
- Game `steam_app_id` is per-game; do not reuse a loop temporary across entries.
