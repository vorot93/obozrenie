# Working conventions

Python 3.11+ GTK 4 / libadwaita game server browser. `pyproject.toml` is the only dependency list.

## Run

- Linux: `sudo apt install adwaita-icon-theme gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 gir1.2-gtk-4.0 gcc libadwaita-1-0 libcairo2-dev libgirepository-2.0-dev libgtk-4-1 pkg-config` — GTK 4, libadwaita, and the cairo/girepository headers uv compiles PyGObject against. Then `uv sync` and `uv run obozrenie-gtk`, or `pip install .` and run the installed `obozrenie-gtk` console entry point. uv builds PyGObject into the venv; distro `python3-gi` is only for a system interpreter.
- macOS: `brew install gtk4 libadwaita gobject-introspection adwaita-icon-theme pkg-config cairo`, then `uv sync` and `uv run obozrenie-gtk`. The launcher re-execs once to set `DYLD_FALLBACK_LIBRARY_PATH` to Homebrew's lib dir. Bare `import obozrenie.gtk` does **not** go through that path — set the env var yourself (CI does this job-wide).
- The launcher refuses GTK below 4.14 and libadwaita below 1.5 (`launch_gtk._require_toolkit`). A missing typelib fails there too, through the `ValueError` `gi.require_version` raises; both end the run on `main()`'s one printed-error path.
- Runtime data lives in `obozrenie/data` and is read through `importlib.resources` streams, independent of cwd and installation prefix. User settings, GeoIP databases, game workdirs, and native library paths remain filesystem paths.

## Test

From the repo root:

    uv run python -m unittest discover -s tests -p 'test*.py' -v

The suites in `tests/test_gtk_widgets.py` build real widgets and need a
display: a desktop session serves, and CI wraps every Linux `unittest` command
in `xvfb-run -a`, with `GSK_RENDERER=cairo` and `GDK_BACKEND=x11` on the jobs
that build widgets. `tests.test_gtk_widgets` raises at import without a display
rather than skipping, so a run that cannot build widgets fails visibly. Nothing
else needs one — importing the GTK tier and loading icon textures do not —
which is why the macOS job runs the packaging install smoke with no display
and no wrapper. Never run with a `DISPLAY` that points at no server: GTK 4
hangs on an unreachable X display instead of failing.

Source catalog completeness and deterministic compiled-catalog check:

    uv run python -m unittest tools.test_translation_tools -v
    uv run python tools/check_translations.py

Build delivery artifacts outside the checkout and run their archive, rebuild,
Python 3.11 install, runtime-discovery, and custom-prefix contracts with both
explicit paths:

    artifacts="$(cd .. && pwd)/obozrenie-dist"
    uv build --clear --out-dir "$artifacts"
    OBOZRENIE_WHEEL="$artifacts/obozrenie-1-py3-none-any.whl" OBOZRENIE_SDIST="$artifacts/obozrenie-1.tar.gz" uv run python -m unittest tests.test_packaging -v

Both variables must name canonical absolute paths to existing files outside
the checkout: the archive contract asserts they are absolute, and the wheel
zip-import check compares them literally against the origins the subprocess
reports, so a relative value — or an absolute one still carrying `..`, as
`$PWD/../obozrenie-dist/...` does — fails the run even though it resolves.
Ordinary discovery omits the artifact work only when both variables are
absent; setting only one variable or setting either to an empty value is an
error.
Those tests compare every shipped source member against a byte map built from
the current working tree, so an artifact built before your latest edit fails
instead of passing on stale bytes: build the pair from the tree you are
testing and rebuild it after every further source change, including catalog
regeneration. The installed and custom-prefix fixtures validate both archives
against that map before they create a virtual environment, and the runtime
suite extracted from the sdist is compared to the checkout tests before it
runs, so a stale artifact and a stale extracted suite cannot validate each
other. The rebuild leg builds a second wheel from the supplied sdist
(`uv build --wheel --clear --out-dir <dir> <sdist>`) and holds it to the same
contract, so a full artifact gate needs both a direct and a rebuilt wheel.
`tests/test_packaging.py` and `tests/test_delivery.py` are source-only. The
packaging helper accepts `OBOZRENIE_TEST_PYTHON` as a canonical `major.minor`
selector (default `3.11`) and proves the created interpreter matches. Its
installed runtime suite comes from the supplied sdist and asserts that imports
resolve under the fresh wheel installation rather than the checkout.
Packaging tests that need a scratch directory create it under
`OBOZRENIE_TEST_TEMP_ROOT`, and remove their own subdirectories within the run.
Without that variable each run allocates a private `mkdtemp` root of its own
under the system temp dir and removes it again at exit when the run left it
empty — a fixed shared name would belong to whichever user created it first
and would outlive every run that used it. An explicit override is the caller's
own directory and is never removed.
Keep `uv.lock` ignored and untracked; do not use `--locked` or `--frozen` for
these source or artifact gates.

Refresh and compile catalogs with `./update_translations.sh` and
`./compile_translations.sh`. Both scripts can be invoked outside the repo cwd.
Catalog tooling uses the development dependency group; runtime test modules do
not import Babel. Stable catalog headers come from
`[tool.obozrenie.translations]` in `pyproject.toml`: change
`pot-creation-date` when source messages intentionally change and
`po-revision-date` when Russian translations intentionally change, then run
both scripts. Babel is exactly pinned in the development dependency group
because its serializer affects catalog bytes; an intentional upgrade must
change that pin, regenerate the catalogs, and rerun the checker.

Bounded-network focused suites:

    .venv/bin/python -m unittest tests.test_network_calls tests.test_geoip tests.test_core_queries -v
    .venv/bin/python -m unittest tests.test_networking.HttpClientTests tests.test_adapters.MinetestAdapterTests tests.test_adapters.RigsOfRodsAdapterTests tests.test_core_queries -v
    .venv/bin/python -m unittest tests.test_networking.RttTests tests.test_core_queries -v
    .venv/bin/python -m unittest tests.tests.EntryPointTests -v

Lifecycle focused suites:

    .venv/bin/python -m unittest tests.test_core_queries.CoreLifecycleTests tests.tests.ApplicationServicesTests -v
    .venv/bin/python -m unittest tests.test_adapters.QStatProcessSupervisorTests -v
    .venv/bin/python -m unittest tests.test_geoip.DatabaseDownloadTests tests.test_gtk_behavior.GeoIPDownloadCoordinatorTests tests.test_gtk_behavior.StartupTests tests.test_gtk_behavior.ApplicationShutdownTests -v

The offline run prints nothing but the unittest summary: a test that reaches
code which logs or prints captures that output
(`mock.patch.object(helpers, "debug_msg")`, `contextlib.redirect_stdout`,
`contextlib.redirect_stderr`) and asserts it, so a stray line in a run is a
test that forgot to.

No test performs real network I/O. `tests/test_networking.py` replaces
`obozrenie.http_client.requests.Session` with its `_FakeSession` seam (a list
of scripted responses/exceptions, or a `request=` handler when the number of
sessions is unpredictable) and patches `obozrenie.http_client.requests.get` to
raise, so any production path that bypasses the owned session fails loudly
instead of dialling out; `_FakeSession` also records whether it was entered
and closed. Adapter suites inherit the same guard from
`_FailClosedHttpAdapterTestCase` in `tests/test_adapters.py`, which patches
`requests.Session` and `requests.get` to raise outright. Route new HTTP-facing
tests through that seam rather than a real endpoint.

`tests/test_transport.py` holds the only suites that run the shipped
transport instead of replacing it — `RealTransportDownloadTests` for the
GeoIP transfer and `RealTransportJsonTests` for `get_json` — because their
bounds and byte-granular body reads are properties of `http.client`,
`urllib3` and `requests` themselves: buffered reads, the buffer lock a close
waits for, the read-side shutdown that does not, and chunked framing and
content decoding under `raw.read1`. They mount a
`requests.adapters.HTTPAdapter` subclass on the owned session whose `send`
builds a real `http.client.HTTPResponse` over a `FakePeer`: an object
implementing the part of the socket protocol those classes use (`makefile`,
`settimeout`, `shutdown`, `close`) and handing out a prepared wire response
one byte per read. No socket, port, or thread of the peer's own is involved,
timings come from the peer's own interval, and every wait is bounded at one
second. A behavioural claim about how fast a read, a deadline, or a
cancellation ends, or about what the shipped stack delivers for a given wire
body, belongs there; a claim about the transfer's own bookkeeping belongs in
`DatabaseDownloadTests` in `tests/test_geoip.py`, whose response fakes cannot
see the transport. Test modules never import one another: the runtime suite
is run by discovery from its own directory, where `tests/tests.py` shadows
the `tests` package name, so shared fixtures live in the module that uses
them.

macOS: any test that reaches `gi` needs the library path the launcher's
re-exec would otherwise set, since a bare import does not go through it. The
modules that do, today: `tests.test_gtk_widgets` (the whole suite);
`tests.test_gtk_behavior` (it imports `obozrenie.gtk`);
`tests.tests.IconLoadingTests`, which imports `gtk_icons` inside its methods;
in `tests.test_resources.ResourceTests`, the two `gtk_icons` uses
(`test_zip_backed_resources_support_text_bytes_toml_ui_and_icons`,
`test_logo_loads_from_zip_resource_bytes`) plus the GdkPixbuf asset check
`test_application_icons_render_at_small_and_large_sizes`; and
`tests.test_presenter` and `tests.test_list_rows`, which reach only `GObject`
and `GLib` through `presenter`/`list_rows` and need no display of any kind:

    DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib" uv run python -m unittest discover -s tests -p 'test*.py' -v

CI gates non-scheduled delivery work separately from the scheduled live HTTP
contracts. One Ubuntu Python 3.11 job runs source/tooling gates and builds one
wheel/sdist pair outside the checkout. Linux 3.11-3.14, macOS 3.13, and the
custom-prefix job download that same pair and invoke the source helper while
asserting installed origins. The weekly schedule runs deterministic delivery
tests and then `python -m tests.live_contracts`; do not run the real live script
locally without an explicit safe request.

QStat is optional; adapter unit tests do not invoke the binary.
Runtime discovery checks `qstat` first and `quakestat` second with
`shutil.which`.

Keep GitHub Actions on maintained major tags (for example,
`actions/checkout@v4`). Do not add container build or delivery files or jobs.
Real live requests belong only to the schedule-owned contract job.

Fixture rules for tests that touch shared runtime state:

- Tests that exercise network workers replace `network_calls._NETWORK_CALL_POOLS` with a fresh `NetworkCallPools`; consumers must resolve that registry dynamically, and tests must not reload `network_calls`. Release in-process blockers in `finally`, join caller threads with a one-second timeout, and call `close_network_call_pools(timeout=0.2)`. Registry fixtures must restore the previous registry in an outer `finally` even if close raises.
- Tests that drive `qstat._run_qstat` or `stat_master` install a fresh `QStatProcessSupervisor` under the same rule, through `installed_qstat_supervisor()` in `tests/test_adapters.py`: it closes its own supervisor against a one-second bound and reinstalls the previous one in an outer `finally`. The module-default supervisor is never closed by a test.
- `Core` initialises its lifecycle state in `__init__`, so Core fixtures construct a real `Core(GameTable(config), geoip.Geolocation())` — the `CoreQueryTestCase.new_core` pattern in `tests/test_core_queries.py` — instead of `Core.__new__(Core)`. A fake country lookup is any object with `countries_for(hosts, deadline=None)`; a fake database is `geoip.Geolocation(reader=…)`.
- Coordinator tests inject a fake `GeoIPOfferDialogs` (`GeoIPDownloadCoordinator(geolocation, dialogs=...)`) and patch `gtk_geoip.GLib.idle_add`, so they run the whole offer/transfer state machine with no display; presenter tests drive `presenter.MainPresenter` over a `FakeView` that answers all of `presenter.MainView`. Only `tests/test_gtk_widgets.py` builds the real widgets behind either seam.

## Layout

- `obozrenie/` — the GTK-free domain (`game_table`, `core`, `geoip`, `settings`, `launch`, `ping`, `models`, `network_calls`, `http_client`, `qstat_processes`, `diagnostics`, `i18n`, `resources`, `helpers`, `global_settings`, `global_strings`, `adapters/`), the presentation tier (`list_rows`, `presenter`: `GObject`/`GLib` only, no display), and the GTK tier (`gtk` = `App`, `gtk_window` = `MainWindow`, `gtk_dialogs`, `gtk_preferences`, `gtk_geoip`, `gtk_lists`, `gtk_icons`, `launch_gtk` = entry point). See the module table at the top of `DESIGN.md`.
- `obozrenie/data/` — packaged UI templates (`main_window.ui`, `server_info_dialog.ui`), settings, game list, icons, desktop entry, and catalogs
- `tests/` — unittest discovery suite

No `docs/superpowers/` (or other process-artifact trees) in this repo.

## Gotchas

- `launch_gtk.main()` is the console-script entry; importing the module must not start GTK. It imports the GTK-free collaborators (`Core`, `GameTable`, `Geolocation`, `Settings`) at module level and `obozrenie.gtk` / `obozrenie.gtk_geoip` inside `main()`, after the macOS re-exec and after `_require_toolkit()`; the launcher tests fake those two modules in `sys.modules`, patch the four names on `launch_gtk`, and patch `_require_toolkit` so they never touch a real toolkit.
- Templates are translated in Python (`gtk_dialogs.load_template`) with the package-stream gettext object before `Gtk.Template` parses them; every visible string lives in the two documents (also `GtkStringList` `<item>` elements), and Python keeps only strings it formats. Do not add native gettext domain/path binding.
- Importing a `gtk_*` module defines its template classes and needs no display; instantiating any widget needs one, and libadwaita widgets need `Adw.init()` first outside an `Adw.Application` (the widget suite calls it). `gtk4-builder-tool validate` cannot resolve libadwaita template parents, so the documents are validated by instantiation in `tests/test_gtk_widgets.py` instead.
- Rigs of Rods master URIs are normalized to exactly one `json=true` query parameter; live API is `https://api.rigsofrods.org/server-list`.
- Game `steam_app_id` is per-game; do not reuse a loop temporary across entries.
- `Settings` creates `<profile path>.lock` beside the profile directory (`~/.config/obozrenie.lock` in a real run) on its first load or save. `filelock` holds it with `flock` and deliberately leaves the file in place after release, so the kernel drops the lock when the process exits: an existing lock file is never stale and must never be deleted to "clear" it — unlinking a locked path splits waiters across inodes and breaks mutual exclusion. On a filesystem without `flock`, `filelock`'s default `fallback_to_soft=True` switches to an existence-marker lock instead, which the library itself unlinks on release and reclaims when its recorded PID is gone — so the rule is the same everywhere, only the mechanism differs: never stale, never delete by hand. Tests that assert on the contents of a profile's parent directory must expect that file.
