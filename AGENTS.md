# Working conventions

Python 3.11+ GTK 3 game server browser. `pyproject.toml` is the only dependency list.

## Run

- Linux: GTK 3 + GObject introspection (`gir1.2-gtk-3.0`, `pkg-config`, cairo/girepository headers). Then `uv sync` and `uv run obozrenie-gtk`, or `pip install .` and run the installed `obozrenie-gtk` console entry point. uv builds PyGObject into the venv; distro `python3-gi` is only for a system interpreter.
- macOS: `brew install gtk+3 gobject-introspection adwaita-icon-theme pkg-config cairo`, then `uv sync` and `uv run obozrenie-gtk`. The launcher re-execs once to set `DYLD_FALLBACK_LIBRARY_PATH` to Homebrew's lib dir. Bare `import obozrenie.gtk` does **not** go through that path — set the env var yourself (CI does this job-wide).
- Runtime data lives in `obozrenie/data` and is read through `importlib.resources` streams, independent of cwd and installation prefix. User settings, GeoIP databases, game workdirs, and native library paths remain filesystem paths.

## Test

From the repo root:

    uv run python -m unittest discover -s tests -p 'test*.py' -v

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
    .venv/bin/python -m unittest tests.test_geoip.DatabaseDownloadTests tests.test_gtk_behavior.GeoIPDownloadCoordinatorTests tests.test_gtk_behavior.ApplicationShutdownTests -v

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

macOS, when the suite imports GTK (`IconLoadingTests`, `CountryEmojiTests`):

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
- `Core` initialises its lifecycle state in `__init__`, so Core fixtures construct a real `Core()` with `resources.load_toml` and `geoip.find_database` patched — the `CoreQueryTestCase.new_core` pattern in `tests/test_core_queries.py` — instead of `Core.__new__(Core)`.

## Layout

- `obozrenie/` — core, GTK UI, launch, GeoIP, adapters (`qstat`, `minetest`, `rigsofrods`)
- `obozrenie/data/` — packaged UI, settings, game list, icons, desktop entry, and catalogs
- `tests/` — unittest discovery suite

No `docs/superpowers/` (or other process-artifact trees) in this repo.

## Gotchas

- `launch_gtk.main()` is the console-script entry; importing the module must not start GTK.
- GtkBuilder resources are translated in Python with the package-stream gettext object before `add_from_string`; do not add native gettext domain/path binding.
- Every widget inside `Main_Window` in `obozrenie/data/ui/obozrenie_gtk.ui` must declare a `visible` property, because activation only calls `present()` and Glade omits that property by default; `BuilderVisibilityTests.test_every_main_window_widget_declares_visible` fails on a widget that lacks it.
- Rigs of Rods master URIs are normalized to exactly one `json=true` query parameter; live API is `https://api.rigsofrods.org/server-list`.
- Game `steam_app_id` is per-game; do not reuse a loop temporary across entries.
- `Settings` creates `<profile path>.lock` beside the profile directory (`~/.config/obozrenie.lock` in a real run) on its first load or save. `filelock` holds it with `flock` and deliberately leaves the file in place after release, so the kernel drops the lock when the process exits: an existing lock file is never stale and must never be deleted to "clear" it — unlinking a locked path splits waiters across inodes and breaks mutual exclusion. On a filesystem without `flock`, `filelock`'s default `fallback_to_soft=True` switches to an existence-marker lock instead, which the library itself unlinks on release and reclaims when its recorded PID is gone — so the rule is the same everywhere, only the mechanism differs: never stale, never delete by hand. Tests that assert on the contents of a profile's parent directory must expect that file.
