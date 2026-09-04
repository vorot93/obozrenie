import ast
import contextlib
import importlib
import io
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from obozrenie import helpers
from obozrenie import settings as settings_module
from obozrenie.diagnostics import SafeDiagnostic
from obozrenie.models import Endpoint


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
LIVE_SCRIPT_PATH = PROJECT_ROOT / "tests" / "live_contracts.py"
SCREENSHOT_URL = (
    "https://raw.githubusercontent.com/vorot93/obozrenie/"
    "master/screenshot.png"
)
LOGO_URL = (
    "https://raw.githubusercontent.com/vorot93/obozrenie/"
    "master/obozrenie/data/icons/hicolor/scalable/apps/obozrenie.svg"
)
TRANSLATION_DEV_HEADING = "## Translation development (Git checkout only)"
TRANSLATION_DEV_EXPLANATION = (
    "These commands are development workflows for a Git checkout. Built wheels and\n"
    "unpacked source distributions intentionally do not include these scripts or\n"
    "tools/.\n"
)
ARTIFACT_NAME = "obozrenie-dist"
ARTIFACT_DIRECTORY = "${{ runner.temp }}/obozrenie-dist"
WHEEL_PATH = f"{ARTIFACT_DIRECTORY}/obozrenie-1-py3-none-any.whl"
SDIST_PATH = f"{ARTIFACT_DIRECTORY}/obozrenie-1.tar.gz"
INSTALLED_METHOD = (
    "tests.test_packaging.InstalledArtifactTests."
    "test_wheel_installs_and_smokes_in_a_fresh_selected_python_venv"
)
PREFIX_METHOD = (
    "tests.test_packaging.InstalledArtifactTests."
    "test_wheel_installs_into_a_clean_custom_prefix"
)
ALLOWED_ACTIONS = {
    "actions/checkout@v4",
    "actions/setup-python@v5",
    "astral-sh/setup-uv@v6",
    "actions/upload-artifact@v4",
    "actions/download-artifact@v4",
}

MINETEST_PAYLOAD = {
    "list": [
        {"malformed": True},
        {
            "address": "minetest.example",
            "port": 30000,
            "clients": 1,
            "clients_max": 20,
            "name": "Minetest contract",
            "gameid": "minetest",
            "clients_list": ["Ada"],
            "password": False,
            "proto_min": 37,
            "proto_max": 46,
        },
    ]
}
RIGSOFRODS_PAYLOAD = [
    {"malformed": True},
    {
        "has-password": False,
        "current-users": 1,
        "max-clients": 8,
        "port": 12000,
        "ip": "rigsofrods.example",
        "name": "Rigs of Rods contract",
        "terrain-name": "any",
        "json-userlist": [{"username": "Lin"}],
        "verified": 1,
        "is-official": 0,
    },
]


def http_status_error(status_code, message_secret, url_secret):
    response = requests.Response()
    response.status_code = status_code
    response.reason = message_secret
    response.url = (
        f"https://user:{url_secret}@status.example/private?token={url_secret}"
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        return error
    raise AssertionError(f"status {status_code} did not raise HTTPError")


def capture_outcome(callback):
    try:
        return callback(), None
    except BaseException as error:
        return None, error


def load_workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def job_commands(job):
    return "\n".join(
        step["run"]
        for step in job["steps"]
        if "run" in step
    )


def action_steps(job):
    return [step for step in job["steps"] if "uses" in step]


class WorkflowStructureTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.workflow = load_workflow()
        self.jobs = self.workflow["jobs"]

    def job(self, name):
        self.assertIn(name, self.jobs, f"workflow job is missing: {name}")
        return self.jobs[name]

    def test_triggers_permissions_and_job_set_are_exact(self):
        self.assertTrue(all(type(key) is str for key in self.workflow))
        self.assertEqual(
            set(self.workflow),
            {"name", "on", "permissions", "jobs"},
        )
        self.assertEqual(
            self.workflow["on"],
            {
                "push": {"branches": ["master"]},
                "pull_request": None,
                "schedule": [{"cron": "17 6 * * 1"}],
            },
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            set(self.jobs),
            {"build", "test", "macos", "custom-prefix", "live-contracts"},
        )

    def test_jobs_have_exact_gates_dependencies_versions_and_finite_timeouts(self):
        delivery_jobs = {"build", "test", "macos", "custom-prefix"}
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertIs(type(job.get("timeout-minutes")), int)
                self.assertGreater(job["timeout-minutes"], 0)
                expected_gate = (
                    "github.event_name != 'schedule'"
                    if name in delivery_jobs
                    else "github.event_name == 'schedule'"
                )
                self.assertEqual(job.get("if"), expected_gate)

        self.assertNotIn("needs", self.job("build"))
        for name in ("test", "macos", "custom-prefix"):
            self.assertEqual(self.job(name).get("needs"), "build")
        self.assertNotIn("needs", self.job("live-contracts"))
        self.assertEqual(self.job("live-contracts")["timeout-minutes"], 5)
        self.assertGreater(
            self.job("live-contracts")["timeout-minutes"] * 60,
            120,
        )

        strategy = self.job("test")["strategy"]
        self.assertIs(strategy["fail-fast"], False)
        self.assertEqual(
            strategy["matrix"],
            {"python-version": ["3.11", "3.12", "3.13", "3.14"]},
        )
        macos_python = [
            step["with"]["python-version"]
            for step in self.job("macos")["steps"]
            if step.get("uses") == "actions/setup-python@v5"
        ]
        self.assertEqual(macos_python, ["3.13"])

    def test_actions_use_only_ruled_maintained_major_tags(self):
        uses = [
            step["uses"]
            for job in self.jobs.values()
            for step in action_steps(job)
        ]
        self.assertTrue(uses)
        self.assertEqual(set(uses), ALLOWED_ACTIONS)
        self.assertEqual(uses.count("actions/upload-artifact@v4"), 1)
        self.assertEqual(uses.count("actions/download-artifact@v4"), 3)

    def test_job_environments_exclude_artifacts_and_step_only_runner_context(self):
        expected_environments = {
            "build": {"GSK_RENDERER": "cairo", "GDK_BACKEND": "x11"},
            "test": {
                "OBOZRENIE_TEST_PYTHON": "${{ matrix.python-version }}",
                "GSK_RENDERER": "cairo",
                "GDK_BACKEND": "x11",
            },
            "macos": {"OBOZRENIE_TEST_PYTHON": "3.13"},
            "custom-prefix": {
                "OBOZRENIE_TEST_PYTHON": "3.11",
                "GSK_RENDERER": "cairo",
                "GDK_BACKEND": "x11",
            },
            "live-contracts": {},
        }
        for name, expected in expected_environments.items():
            with self.subTest(job=name):
                environment = self.job(name).get("env", {})
                self.assertEqual(environment, expected)
                self.assertNotIn("${{ runner.", json.dumps(environment))

    def test_build_creates_and_uploads_one_canonical_artifact_pair(self):
        artifact_environment = {
            "OBOZRENIE_WHEEL": WHEEL_PATH,
            "OBOZRENIE_SDIST": SDIST_PATH,
        }
        consumers = {
            "build": "Validate canonical archives and sdist rebuild",
            "test": "Test the installed wheel and runtime suite",
            "macos": "Test the installed wheel, runtime suite, GTK, and resources",
            "custom-prefix": "Test a clean custom-prefix installation",
        }
        for job_name, step_name in consumers.items():
            steps = [
                step
                for step in self.job(job_name)["steps"]
                if step.get("name") == step_name
            ]
            with self.subTest(job=job_name):
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0].get("env"), artifact_environment)

        commands = {
            name: job_commands(job) for name, job in self.jobs.items()
        }
        self.assertEqual(
            sum(command.count("uv build --clear") for command in commands.values()),
            1,
        )
        self.assertIn(
            'uv build --clear --out-dir "${{ runner.temp }}/obozrenie-dist"',
            commands["build"],
        )
        for name in ("test", "macos", "custom-prefix", "live-contracts"):
            with self.subTest(job=name):
                self.assertNotIn("uv build", commands[name])

        uploads = [
            step
            for step in self.job("build")["steps"]
            if step.get("uses") == "actions/upload-artifact@v4"
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["with"]["name"], ARTIFACT_NAME)
        self.assertEqual(
            uploads[0]["with"]["path"].splitlines(),
            [WHEEL_PATH, SDIST_PATH],
        )
        self.assertEqual(uploads[0]["with"]["if-no-files-found"], "error")

        for name in ("test", "macos", "custom-prefix"):
            downloads = [
                step
                for step in self.job(name)["steps"]
                if step.get("uses") == "actions/download-artifact@v4"
            ]
            with self.subTest(job=name):
                self.assertEqual(len(downloads), 1)
                self.assertEqual(
                    downloads[0]["with"],
                    {"name": ARTIFACT_NAME, "path": ARTIFACT_DIRECTORY},
                )

    def test_build_runs_all_source_and_exact_archive_gates(self):
        commands = job_commands(self.job("build"))
        required = (
            "uv sync",
            "python -m unittest discover -s tests -p 'test*.py' -v",
            "python -m unittest tools.test_translation_tools -v",
            "python tools/check_translations.py",
            "python -m compileall -q obozrenie tests tools",
            "xvfb-run -a uv run python -m unittest discover -s tests -p 'test*.py' -v",
            'python -c "import obozrenie.gtk"',
            "uv pip check",
            "tests.test_packaging.WheelContractMutationTests",
            "tests.test_packaging.SdistContractMutationTests",
            "tests.test_packaging.ArchiveTests",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, commands)

        all_commands = "\n".join(
            job_commands(job) for job in self.jobs.values()
        )
        self.assertNotIn("--locked", all_commands)
        self.assertNotIn("--frozen", all_commands)

    def test_source_discovery_runs_without_artifact_environment_workaround(self):
        source_steps = [
            step
            for step in self.job("build")["steps"]
            if step.get("name") == "Run source tests and delivery structure checks"
        ]
        self.assertEqual(len(source_steps), 1)
        self.assertEqual(
            source_steps[0]["run"],
            "xvfb-run -a uv run python -m unittest discover -s tests -p 'test*.py' -v",
        )

    def test_installed_jobs_use_the_selected_helper_without_checkout_imports(self):
        linux = job_commands(self.job("test"))
        macos = job_commands(self.job("macos"))
        prefix = job_commands(self.job("custom-prefix"))
        for name, commands in (("test", linux), ("macos", macos)):
            with self.subTest(job=name):
                self.assertIn(INSTALLED_METHOD, commands)
                self.assertNotIn("unittest discover", commands)
                self.assertNotIn("uv sync", commands)
                self.assertNotIn("pip install .", commands)
        self.assertEqual(
            self.job("test").get("env", {}).get("OBOZRENIE_TEST_PYTHON"),
            "${{ matrix.python-version }}",
        )
        self.assertEqual(
            self.job("macos").get("env", {}).get("OBOZRENIE_TEST_PYTHON"),
            "3.13",
        )
        self.assertIn(PREFIX_METHOD, prefix)
        self.assertNotIn(INSTALLED_METHOD, prefix)
        self.assertNotIn("uv sync", prefix)
        self.assertEqual(
            self.job("custom-prefix").get("env", {}).get(
                "OBOZRENIE_TEST_PYTHON"
            ),
            "3.11",
        )

    def test_macos_loader_is_job_wide_and_live_execution_is_schedule_only(self):
        loader_steps = [
            step
            for step in self.job("macos")["steps"]
            if "DYLD_FALLBACK_LIBRARY_PATH" in step.get("run", "")
        ]
        self.assertEqual(len(loader_steps), 1)
        self.assertIn("brew --prefix", loader_steps[0]["run"])
        self.assertIn("$GITHUB_ENV", loader_steps[0]["run"])

        live_commands = job_commands(self.job("live-contracts"))
        self.assertIn("uv sync", live_commands)
        self.assertIn("python -m unittest tests.test_delivery", live_commands)
        self.assertIn("python -m tests.live_contracts", live_commands)

    def test_linux_test_commands_run_under_a_virtual_display(self):
        for name in ("build", "test", "custom-prefix", "live-contracts"):
            commands = [
                command
                for command in job_commands(self.job(name)).splitlines()
                if "unittest" in command
            ]
            with self.subTest(job=name):
                self.assertTrue(commands)
                for command in commands:
                    self.assertIn("xvfb-run -a", command)
        self.assertNotIn("xvfb-run", job_commands(self.job("macos")))
        for name in self.jobs:
            self.assertNotIn("gtk-builder-tool", job_commands(self.job(name)))


class LegacyDeliveryRemovalTests(unittest.TestCase):
    def test_legacy_container_files_job_docs_and_root_wrapper_are_absent(self):
        product = "".join(("dock", "er"))
        for relative_path in (
            product.title() + "file",
            "." + product + "ignore",
            "obozrenie-gtk",
        ):
            with self.subTest(path=relative_path):
                self.assertFalse((PROJECT_ROOT / relative_path).exists())

        workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8").lower()
        readme_text = (PROJECT_ROOT / "README.md").read_text(
            encoding="utf-8"
        ).lower()
        agents_text = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for term in (product, "container:", "image:", "registry"):
            with self.subTest(term=term):
                self.assertNotIn(term, workflow_text)
                self.assertNotIn(term, readme_text)
        self.assertNotIn("./obozrenie-gtk", agents_text)
        self.assertNotIn("Linux CI: `pip install .`", agents_text)
        self.assertNotIn("macOS CI: `uv sync`", agents_text)

        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)
        self.assertEqual(
            project["project"]["scripts"],
            {"obozrenie-gtk": "obozrenie.launch_gtk:main"},
        )


class RepositoryDocumentationTests(unittest.TestCase):
    """README distribution claims must match what wheels/sdists ship."""

    def readme_text(self):
        return (PROJECT_ROOT / "README.md").read_bytes().decode("utf-8")

    def test_image_links_are_durable_absolute_repository_urls(self):
        readme = self.readme_text()
        self.assertIn(f"![]({SCREENSHOT_URL})", readme)
        self.assertIn(f"![Obozrenie]({LOGO_URL})", readme)
        # A relative link only resolves on GitHub's own file browser, not for
        # a PyPI long-description or an unpacked sdist reader.
        self.assertNotIn("](screenshot.png)", readme)
        self.assertNotIn("](obozrenie/data/", readme)

    def test_translation_regeneration_is_labeled_a_checkout_only_workflow(self):
        readme = self.readme_text()
        self.assertIn(TRANSLATION_DEV_HEADING, readme)
        self.assertIn(TRANSLATION_DEV_EXPLANATION, readme)

        heading_at = readme.index(TRANSLATION_DEV_HEADING)
        explanation_at = readme.index(TRANSLATION_DEV_EXPLANATION)
        commands_at = readme.index("./update_translations.sh")
        # The heading must introduce the disclaimer, and the disclaimer must
        # precede the very commands it says are absent from built artifacts.
        self.assertLess(heading_at, explanation_at)
        self.assertLess(explanation_at, commands_at)

    def test_native_install_and_run_instructions_stay_wheel_usable(self):
        readme = self.readme_text()
        # These must keep working from wheel/sdist metadata alone: they must
        # not depend on the checkout-only translation tooling.
        self.assertIn("python -m pip install .", readme)
        self.assertIn("uv run obozrenie-gtk", readme)

    def test_universal_language_selection_guidance_is_not_labeled_checkout_only(
        self,
    ):
        readme = self.readme_text()
        marker = "LANGUAGE=ru_RU obozrenie-gtk"
        self.assertIn(marker, readme)

        # This example works from any install (the Russian catalog ships in
        # the wheel too), so it must stay under a heading that does not claim
        # it is a Git-checkout-only development workflow: find the nearest
        # "## " heading above it and check that heading, not just its text.
        heading = None
        for line in readme[: readme.index(marker)].splitlines():
            if line.startswith("## "):
                heading = line
        self.assertIsNotNone(heading)
        self.assertNotEqual(heading, TRANSLATION_DEV_HEADING)
        self.assertNotIn("checkout", heading.lower())


class RepositoryHygieneTests(unittest.TestCase):
    """Compatibility-era dead code and stale defaults must stay removed."""

    # obozrenie/ referenced none of these; the data/UI settings work replaced
    # their last caller (`load_table`) with `load_user_table`. `deepcopy` lost
    # its last caller when the game table moved to its own module and its call
    # sites switched to `copy.deepcopy` directly (see obozrenie/game_table.py).
    # `sort_dict_table` and `dict_to_list` built GTK 3 list-store rows; the
    # GTK 4 layer builds `list_rows` objects instead, so they stay dead here.
    DEAD_HELPERS = (
        "Bunch",
        "enum",
        "search_table",
        "search_dict_table",
        "flatten_dict_table",
        "flatten",
        "flatten_list",
        "remove_all_occurences_from_list",
        "load_table",
        "deepcopy",
        "sort_dict_table",
        "dict_to_list",
    )
    # The exact set `obozrenie/` still calls `helpers.<name>` for.
    RETAINED_HELPERS = (
        "debug_msg",
        "log_safe_diagnostic",
        "enforce_array",
        "load_user_table",
        "save_table",
    )

    def test_dead_compatibility_helpers_are_absent(self):
        for name in self.DEAD_HELPERS:
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(helpers, name),
                    f"dead compatibility helper {name!r} is still defined",
                )

    def test_referenced_production_helpers_are_retained(self):
        for name in self.RETAINED_HELPERS:
            with self.subTest(name=name):
                self.assertTrue(
                    callable(getattr(helpers, name, None)),
                    f"helper {name!r} referenced by obozrenie/ is missing",
                )

    def test_common_defaults_match_common_options_exactly(self):
        defaults_path = (
            PROJECT_ROOT
            / "obozrenie"
            / "data"
            / "settings"
            / "defaults"
            / "settings.toml"
        )
        defaults = tomllib.loads(defaults_path.read_bytes().decode("utf-8"))

        declared = set(settings_module.COMMON_OPTIONS["common"])
        self.assertEqual(set(defaults["common"]), declared)

    def test_gtk_module_does_not_import_signal(self):
        # The GTK layer used to be one module; it is now `gtk.py` plus the
        # window, GeoIP offer, and preferences modules it split into, so the
        # scan must cover all of them or a later split silently drops it.
        gtk_module_paths = sorted((PROJECT_ROOT / "obozrenie").glob("gtk*.py"))
        self.assertTrue(gtk_module_paths, "no obozrenie/gtk*.py modules found")
        for path in gtk_module_paths:
            relative_path = path.relative_to(PROJECT_ROOT)
            with self.subTest(path=relative_path):
                gtk_source = path.read_bytes().decode("utf-8")
                self.assertNotIn(
                    "\nimport signal\n",
                    f"\n{gtk_source}",
                    f"{relative_path} must not import the unused `signal` module",
                )


class LiveContractTests(unittest.TestCase):
    # What the script prints to stderr for two retried attempts: only an
    # attempt that is retried announces a retry, so the third never does.
    RETRIES_BEFORE_THIRD_ATTEMPT = (
        "Minetest transient failure on attempt 1; retrying.\n"
        "Minetest transient failure on attempt 2; retrying.\n"
    )

    def live_contracts(self):
        self.assertTrue(LIVE_SCRIPT_PATH.is_file(), "live contract script is missing")
        module_name = "_obozrenie_live_contracts_test"
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(
            module_name,
            LIVE_SCRIPT_PATH,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def test_configured_https_payloads_parse_through_production_adapters(self):
        live = self.live_contracts()

        def response_for(url, **_kwargs):
            if url == "https://servers.minetest.net/list":
                return MINETEST_PAYLOAD
            if url == "https://api.rigsofrods.org/server-list?json=true":
                return RIGSOFRODS_PAYLOAD
            raise AssertionError(f"unexpected live-contract URL: {url}")

        with (
            mock.patch.object(live.time, "monotonic", return_value=100.0),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=response_for,
            ) as get_json,
        ):
            results = live.run_contracts()

        self.assertEqual(set(results), {"minetest", "rigsofrods"})
        self.assertEqual(len(results["minetest"]), 1)
        self.assertEqual(
            results["minetest"][0].endpoint,
            Endpoint("minetest.example", 30000),
        )
        self.assertEqual(results["minetest"][0].players[0].name, "Ada")
        self.assertEqual(len(results["rigsofrods"]), 1)
        self.assertEqual(
            results["rigsofrods"][0].endpoint,
            Endpoint("rigsofrods.example", 12000),
        )
        self.assertEqual(results["rigsofrods"][0].players[0].name, "Lin")
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://servers.minetest.net/list",
                    deadline=130.0,
                ),
                mock.call(
                    "https://api.rigsofrods.org/server-list?json=true",
                    deadline=130.0,
                ),
            ],
        )

    def test_retry_allowlist_accepts_only_timeout_connection_integrity_decode_and_selected_statuses(
        self,
    ):
        live = self.live_contracts()
        transient_statuses = {429, 500, 502, 503, 504}
        cases = (
            ("timeout", requests.Timeout("timeout-message-secret")),
            (
                "connection",
                requests.ConnectionError("connection-message-secret"),
            ),
            (
                "chunked response",
                requests.exceptions.ChunkedEncodingError(
                    "chunked-message-secret"
                ),
            ),
            (
                "content decoding",
                requests.exceptions.ContentDecodingError(
                    "content-decoding-message-secret"
                ),
            ),
            (
                "JSON decode",
                json.JSONDecodeError(
                    "json-message-secret",
                    '{"token": "json-document-secret"}',
                    1,
                ),
            ),
            (
                "Unicode decode",
                UnicodeDecodeError(
                    "utf-8",
                    b"\xff",
                    0,
                    1,
                    "unicode-message-secret",
                ),
            ),
            (
                "unknown encoding",
                live.http_client.UnknownEncodingError(
                    SafeDiagnostic("encoding-message-secret")
                ),
            ),
            (
                "response decode",
                live.http_client.ResponseDecodeError(
                    SafeDiagnostic("decode-message-secret")
                ),
            ),
            *(
                (
                    f"status {status_code}",
                    http_status_error(
                        status_code,
                        f"status-{status_code}-message-secret",
                        f"status-{status_code}-url-secret",
                    ),
                )
                for status_code in sorted(transient_statuses)
            ),
        )

        for name, error in cases:
            with self.subTest(failure=name):
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        live.time,
                        "monotonic",
                        side_effect=[100.0, 100.5, 101.0, 101.5],
                    ),
                    mock.patch.object(
                        live.http_client,
                        "get_json",
                        side_effect=[error, MINETEST_PAYLOAD],
                    ) as get_json,
                    contextlib.redirect_stderr(stderr),
                ):
                    servers, raised = capture_outcome(
                        lambda: live.fetch_validated(
                            "Minetest",
                            "https://servers.minetest.net/list",
                            live.validate_minetest,
                            220.0,
                        )
                    )

                self.assertIsNone(
                    raised,
                    f"{name} raised {type(raised).__name__} instead of retrying",
                )
                self.assertEqual(len(servers), 1)
                self.assertEqual(len(get_json.call_args_list), 2)
                self.assertEqual(
                    stderr.getvalue(),
                    "Minetest transient failure on attempt 1; retrying.\n",
                )

        actual_transient_statuses = {
            status_code
            for status_code in range(400, 600)
            if live._is_transient(
                http_status_error(
                    status_code,
                    f"enumerated-{status_code}-message-secret",
                    f"enumerated-{status_code}-url-secret",
                )
            )
        }
        self.assertEqual(actual_transient_statuses, transient_statuses)

    def test_tls_invalid_request_redirect_generic_request_and_permanent_status_fail_after_one_attempt(
        self,
    ):
        live = self.live_contracts()

        class TlsTimeoutError(requests.exceptions.SSLError, requests.Timeout):
            pass

        class PoolTimeoutError(
            live.network_calls.CallDeadlineExceeded,
            requests.Timeout,
        ):
            pass

        cases = (
            ("TLS", TlsTimeoutError("tls-message-secret")),
            (
                "pool infrastructure",
                PoolTimeoutError("pool-message-secret"),
            ),
            (
                "invalid request",
                requests.exceptions.InvalidURL("invalid-url-message-secret"),
            ),
            (
                "redirect request",
                requests.exceptions.TooManyRedirects(
                    "redirect-message-secret"
                ),
            ),
            (
                "HTTPS policy",
                live.http_client.HttpsPolicyError(
                    SafeDiagnostic("https-policy-message-secret")
                ),
            ),
            (
                "generic request",
                requests.RequestException("request-message-secret"),
            ),
            (
                "response too large",
                live.http_client.ResponseTooLargeError(
                    SafeDiagnostic("size-message-secret")
                ),
            ),
            ("unknown", RuntimeError("unknown-message-secret")),
            (
                "permanent status",
                http_status_error(
                    404,
                    "status-404-message-secret",
                    "status-404-url-secret",
                ),
            ),
        )
        for name, error in cases:
            with self.subTest(failure=name):
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        live.time,
                        "monotonic",
                        return_value=100.0,
                    ),
                    mock.patch.object(
                        live.http_client,
                        "get_json",
                        side_effect=error,
                    ) as get_json,
                    contextlib.redirect_stderr(stderr),
                ):
                    _result, raised = capture_outcome(
                        lambda: live.fetch_validated(
                            "Minetest",
                            "https://servers.minetest.net/list",
                            live.validate_minetest,
                            220.0,
                        )
                    )

                self.assertIs(raised, error)
                self.assertEqual(len(get_json.call_args_list), 1)
                self.assertEqual(stderr.getvalue(), "")

    def test_invalid_url_preflight_is_payload_free_and_makes_no_attempt(self):
        live = self.live_contracts()
        secret = "invalid-preflight-secret"
        invalid_url = (
            f"http://user:{secret}@plaintext.invalid/private?token={secret}"
        )
        stderr = io.StringIO()
        real_validate_https_url = live.http_client.validate_https_url
        with (
            mock.patch.object(
                live.http_client,
                "validate_https_url",
                wraps=real_validate_https_url,
            ) as validate_https_url,
            mock.patch.object(live.http_client, "get_json") as get_json,
            contextlib.redirect_stderr(stderr),
        ):
            _result, raised = capture_outcome(
                lambda: live.fetch_validated(
                    "Minetest",
                    invalid_url,
                    live.validate_minetest,
                    220.0,
                )
            )

        self.assertIs(type(raised), live.http_client.HttpsPolicyError)
        validate_https_url.assert_called_once_with(invalid_url)
        self.assertEqual(len(get_json.call_args_list), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            str(raised),
            "HTTPS URL policy rejected the request.",
        )
        self.assertNotIn(secret, str(raised))
        self.assertNotIn(secret, repr(raised))
        self.assertIsNone(raised.__cause__)
        self.assertIsNone(raised.__context__)

    def test_retry_and_exhaustion_diagnostics_omit_exception_and_url_payload(self):
        live = self.live_contracts()
        secrets = (
            "attempt-1-message-secret",
            "attempt-1-url-secret",
            "attempt-2-message-secret",
            "attempt-2-url-secret",
            "attempt-3-message-secret",
            "attempt-3-url-secret",
        )
        errors = [
            http_status_error(503, message_secret, url_secret)
            for message_secret, url_secret in zip(
                secrets[::2],
                secrets[1::2],
                strict=True,
            )
        ]
        stderr = io.StringIO()
        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[190.0, 191.0, 200.0, 201.0, 210.0, 211.0],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=errors,
            ) as get_json,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(live.LiveContractError) as raised,
        ):
            live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )

        self.assertEqual(len(get_json.call_args_list), 3)
        self.assertEqual(
            stderr.getvalue(),
            "Minetest transient failure on attempt 1; retrying.\n"
            "Minetest transient failure on attempt 2; retrying.\n",
        )
        self.assertEqual(
            str(raised.exception),
            "Minetest live contract failed after 3 attempts.",
        )
        rendered_diagnostics = {
            "stderr": stderr.getvalue(),
            "exception": str(raised.exception),
            "cause": (
                ""
                if raised.exception.__cause__ is None
                else str(raised.exception.__cause__)
            ),
            "context": (
                ""
                if raised.exception.__context__ is None
                else str(raised.exception.__context__)
            ),
        }
        for diagnostic_name, diagnostic in rendered_diagnostics.items():
            for secret in secrets:
                with self.subTest(
                    diagnostic=diagnostic_name,
                    secret=secret,
                ):
                    self.assertNotIn(secret, diagnostic)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_attempt_and_script_deadlines_remain_unchanged_under_retry(self):
        live = self.live_contracts()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[100.0, 100.5, 101.0, 101.5, 219.0, 219.5],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=[
                    requests.Timeout("first-deadline-message-secret"),
                    http_status_error(
                        503,
                        "second-deadline-message-secret",
                        "second-deadline-url-secret",
                    ),
                    MINETEST_PAYLOAD,
                ],
            ) as get_json,
            contextlib.redirect_stderr(stderr),
        ):
            servers = live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(
            [call.kwargs["deadline"] for call in get_json.call_args_list],
            [130.0, 131.0, 220.0],
        )

    def test_transport_and_decode_failures_stop_after_exactly_three_attempts(self):
        live = self.live_contracts()
        failures = (
            (
                "transport",
                lambda index: requests.ConnectionError(f"offline {index}"),
            ),
            (
                "decode",
                lambda index: json.JSONDecodeError(
                    f"bad json {index}",
                    "{",
                    1,
                ),
            ),
            (
                "unicode decode",
                lambda index: UnicodeDecodeError(
                    "utf-8",
                    b"\xff",
                    0,
                    1,
                    f"invalid text {index}",
                ),
            ),
            (
                "unknown character encoding",
                lambda _index: live.http_client.UnknownEncodingError(
                    SafeDiagnostic(
                        "HTTP response uses an unknown character encoding"
                    )
                ),
            ),
        )
        for name, make_error in failures:
            with self.subTest(failure=name):
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        live.time,
                        "monotonic",
                        side_effect=[190.0, 191.0, 200.0, 201.0, 210.0, 211.0],
                    ),
                    mock.patch.object(
                        live.http_client,
                        "get_json",
                        side_effect=[make_error(index) for index in range(3)],
                    ) as get_json,
                    contextlib.redirect_stderr(stderr),
                    self.assertRaisesRegex(
                        live.LiveContractError,
                        "Minetest.*3 attempts",
                    ),
                ):
                    live.fetch_validated(
                        "Minetest",
                        "https://servers.minetest.net/list",
                        live.validate_minetest,
                        220.0,
                    )

                self.assertEqual(len(get_json.call_args_list), 3)
                self.assertEqual(
                    stderr.getvalue(),
                    self.RETRIES_BEFORE_THIRD_ATTEMPT,
                )
                self.assertEqual(
                    [call.kwargs for call in get_json.call_args_list],
                    [
                        {"deadline": 220.0},
                        {"deadline": 220.0},
                        {"deadline": 220.0},
                    ],
                )

    def test_transient_failures_can_recover_on_the_third_attempt(self):
        live = self.live_contracts()
        decode_error = json.JSONDecodeError("bad json", "{", 1)
        stderr = io.StringIO()
        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[100.0, 100.5, 101.0, 101.5, 102.0, 102.5],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=[
                    requests.Timeout("slow"),
                    decode_error,
                    MINETEST_PAYLOAD,
                ],
            ) as get_json,
            contextlib.redirect_stderr(stderr),
        ):
            servers = live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(
            [call.kwargs["deadline"] for call in get_json.call_args_list],
            [130.0, 131.0, 132.0],
        )
        self.assertEqual(stderr.getvalue(), self.RETRIES_BEFORE_THIRD_ATTEMPT)

    def test_text_decode_failures_can_recover_on_the_third_attempt(self):
        live = self.live_contracts()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[100.0, 100.5, 101.0, 101.5, 102.0, 102.5],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=[
                    UnicodeDecodeError(
                        "utf-8",
                        b"\xff",
                        0,
                        1,
                        "invalid response text",
                    ),
                    live.http_client.UnknownEncodingError(
                        SafeDiagnostic(
                            "HTTP response uses an unknown character encoding"
                        )
                    ),
                    MINETEST_PAYLOAD,
                ],
            ) as get_json,
            contextlib.redirect_stderr(stderr),
        ):
            servers = live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(
            [call.kwargs["deadline"] for call in get_json.call_args_list],
            [130.0, 131.0, 132.0],
        )
        self.assertEqual(stderr.getvalue(), self.RETRIES_BEFORE_THIRD_ATTEMPT)

    def test_decoded_schema_and_all_malformed_entries_fail_without_retry(self):
        live = self.live_contracts()
        cases = (
            ("Minetest schema", live.validate_minetest, []),
            ("Minetest entries", live.validate_minetest, {"list": [{}]}),
            ("Rigs schema", live.validate_rigsofrods, {"servers": []}),
            ("Rigs entries", live.validate_rigsofrods, [{}]),
        )
        for name, validator, payload in cases:
            with self.subTest(case=name):
                with (
                    mock.patch.object(live.time, "monotonic", return_value=100.0),
                    mock.patch.object(
                        live.http_client,
                        "get_json",
                        return_value=payload,
                    ) as get_json,
                    self.assertRaisesRegex(
                        live.LiveContractError,
                        "response|valid",
                    ),
                ):
                    live.fetch_validated(
                        name,
                        "https://contract.example/list",
                        validator,
                        220.0,
                    )
                self.assertEqual(len(get_json.call_args_list), 1)

    def test_non_decode_value_errors_are_not_retried(self):
        live = self.live_contracts()
        for name, error in (
            ("generic", ValueError("unexpected response value")),
            ("oversize", ValueError("HTTP response exceeds 16777216 bytes")),
        ):
            with self.subTest(error=name):
                with (
                    mock.patch.object(
                        live.time,
                        "monotonic",
                        return_value=100.0,
                    ),
                    mock.patch.object(
                        live.http_client,
                        "get_json",
                        side_effect=error,
                    ) as get_json,
                    self.assertRaises(ValueError) as raised,
                ):
                    live.fetch_validated(
                        "Minetest",
                        "https://servers.minetest.net/list",
                        live.validate_minetest,
                        220.0,
                    )
                self.assertIs(raised.exception, error)
                self.assertEqual(len(get_json.call_args_list), 1)

    def test_shared_script_deadline_stops_before_or_between_attempts(self):
        live = self.live_contracts()
        with (
            mock.patch.object(live.time, "monotonic", return_value=220.0),
            mock.patch.object(live.http_client, "get_json") as get_json,
            self.assertRaisesRegex(
                live.ScriptDeadlineExceeded,
                "script deadline",
            ),
        ):
            live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )
        self.assertEqual(len(get_json.call_args_list), 0)

        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[219.0, 220.0],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                side_effect=requests.Timeout("slow"),
            ) as get_json,
            self.assertRaisesRegex(
                live.ScriptDeadlineExceeded,
                "script deadline",
            ),
        ):
            live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )
        self.assertEqual(len(get_json.call_args_list), 1)
        self.assertEqual(get_json.call_args.kwargs, {"deadline": 220.0})

    def test_shared_script_deadline_covers_decoded_validation(self):
        live = self.live_contracts()
        with (
            mock.patch.object(
                live.time,
                "monotonic",
                side_effect=[219.0, 220.0],
            ),
            mock.patch.object(
                live.http_client,
                "get_json",
                return_value=MINETEST_PAYLOAD,
            ) as get_json,
            self.assertRaisesRegex(
                live.ScriptDeadlineExceeded,
                "script deadline",
            ),
        ):
            live.fetch_validated(
                "Minetest",
                "https://servers.minetest.net/list",
                live.validate_minetest,
                220.0,
            )
        self.assertEqual(len(get_json.call_args_list), 1)
        self.assertEqual(get_json.call_args.kwargs, {"deadline": 220.0})

    def test_main_closes_pools_once_after_success_exception_and_base_exception(self):
        live = self.live_contracts()
        success = {"minetest": [], "rigsofrods": []}
        stdout = io.StringIO()
        with (
            mock.patch.object(live, "run_contracts", return_value=success),
            mock.patch.object(
                live.network_calls,
                "close_network_call_pools",
            ) as close,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertIs(live.main(), success)
        self.assertEqual(len(close.call_args_list), 1)
        self.assertEqual(
            stdout.getvalue(),
            "minetest: 0 valid server record(s)\n"
            "rigsofrods: 0 valid server record(s)\n",
        )

        class StopLiveContract(BaseException):
            pass

        for error in (RuntimeError("failed"), StopLiveContract("stopped")):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(
                        live,
                        "run_contracts",
                        side_effect=error,
                    ),
                    mock.patch.object(
                        live.network_calls,
                        "close_network_call_pools",
                    ) as close,
                    self.assertRaises(type(error)) as raised,
                ):
                    live.main()
                self.assertIs(raised.exception, error)
                self.assertEqual(len(close.call_args_list), 1)

    def test_main_preserves_primary_failure_when_pool_close_also_fails(self):
        live = self.live_contracts()
        primary = RuntimeError("primary")
        cleanup = RuntimeError("cleanup")
        with (
            mock.patch.object(live, "run_contracts", side_effect=primary),
            mock.patch.object(
                live.network_calls,
                "close_network_call_pools",
                side_effect=cleanup,
            ) as close,
            self.assertRaises(RuntimeError) as raised,
        ):
            live.main()
        self.assertIs(raised.exception, primary)
        self.assertEqual(len(close.call_args_list), 1)

    def test_script_imports_only_http_resource_parser_and_cleanup_boundaries(self):
        self.live_contracts()
        tree = ast.parse(LIVE_SCRIPT_PATH.read_text(encoding="utf-8"))
        imports = set()
        calls = set()
        references = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    calls.add(function.id)
                elif isinstance(function, ast.Attribute):
                    calls.add(function.attr)
            elif isinstance(node, ast.Attribute):
                references.add(node.attr)

        forbidden_imports = {
            "core",
            "geoip",
            "gtk",
            "icmplib",
            "launch",
            "launch_gtk",
            "ping",
            "qstat",
            "socket",
            "subprocess",
        }
        self.assertTrue(forbidden_imports.isdisjoint(imports))
        self.assertTrue(
            {
                "Core",
                "add_geoip_info",
                "add_rtt_info",
                "getaddrinfo_many_until",
                "measure_rtt",
                "stat_master",
                "sleep",
            }.isdisjoint(calls)
        )
        self.assertTrue(
            {
                "close_network_call_pools",
                "get_json",
                "load_toml",
                "master_request_url",
                "parse_json_entry",
                "parse_server_entry",
            }.issubset(calls | references)
        )

    def test_script_import_does_not_initialize_forbidden_production_paths(self):
        script = """
import json
import sys

import tests.live_contracts

forbidden = {
    'obozrenie.adapters.qstat',
    'obozrenie.core',
    'obozrenie.geoip',
    'obozrenie.gtk',
    'obozrenie.launch',
    'obozrenie.launch_gtk',
    'obozrenie.ping',
}
print(json.dumps(sorted(forbidden.intersection(sys.modules))))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["[]"],
            f"unexpected live import side effects:\n{result.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
