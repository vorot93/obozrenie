import atexit
import base64
import copy
import configparser
import csv
import email.policy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import warnings
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT_VARIABLE = "OBOZRENIE_TEST_TEMP_ROOT"


def _allocate_temp_root():
    """Return this run's scratch root and whether this run owns it.

    A fixed name under the shared system temporary directory belongs to
    whichever user creates it first and outlives every run that used it.
    `mkdtemp` gives each run a private `0700` directory of its own instead,
    which the cleanup below removes again when the run leaves it empty. An
    explicit override stays the caller's own directory, created if need be
    and never removed here.
    """
    override = os.environ.get(TEMP_ROOT_VARIABLE)
    if override:
        return Path(override), False
    return Path(tempfile.mkdtemp(prefix="obozrenie-tests-")), True


# Tool-neutral bounded scratch root for these tests' own temporary
# directories; override it with OBOZRENIE_TEST_TEMP_ROOT (see AGENTS.md).
TEMP_ROOT, _OWNS_TEMP_ROOT = _allocate_temp_root()


def _remove_owned_temp_root():
    """Remove this run's own scratch root, and only while it is empty.

    Anything a test deliberately left behind is worth more than the empty
    directory, so a non-empty root simply stays.
    """
    try:
        TEMP_ROOT.rmdir()
    except OSError:
        pass


if _OWNS_TEMP_ROOT:
    atexit.register(_remove_owned_temp_root)
WHEEL_VARIABLE = "OBOZRENIE_WHEEL"
SDIST_VARIABLE = "OBOZRENIE_SDIST"
TEST_PYTHON_VARIABLE = "OBOZRENIE_TEST_PYTHON"


@dataclass(frozen=True)
class ArtifactInputState:
    wheel: str | None
    sdist: str | None
    should_skip_artifact_tests: bool
    error: str | None

    def assert_valid(self):
        if self.error is not None:
            raise AssertionError(self.error)

    def require_artifacts(self):
        self.assert_valid()
        if self.should_skip_artifact_tests:
            raise AssertionError("artifact inputs are not configured")
        return self.wheel, self.sdist


def evaluate_artifact_inputs(environment):
    wheel_present = WHEEL_VARIABLE in environment
    sdist_present = SDIST_VARIABLE in environment
    wheel = environment[WHEEL_VARIABLE] if wheel_present else None
    sdist = environment[SDIST_VARIABLE] if sdist_present else None

    if not wheel_present and not sdist_present:
        return ArtifactInputState(None, None, True, None)
    if wheel_present != sdist_present:
        return ArtifactInputState(
            wheel,
            sdist,
            False,
            f"set both {WHEEL_VARIABLE} and {SDIST_VARIABLE}, or neither",
        )
    if (
        not isinstance(wheel, str)
        or not isinstance(sdist, str)
        or not wheel
        or not sdist
    ):
        return ArtifactInputState(
            wheel,
            sdist,
            False,
            f"{WHEEL_VARIABLE} and {SDIST_VARIABLE} must have non-empty values",
        )
    return ArtifactInputState(wheel, sdist, False, None)


ARTIFACT_INPUTS = evaluate_artifact_inputs(os.environ)


def select_test_python(environment):
    value = environment.get(TEST_PYTHON_VARIABLE, "3.11")
    if type(value) is not str or re.fullmatch(
        r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)",
        value,
    ) is None:
        raise AssertionError(
            f"{TEST_PYTHON_VARIABLE} must be a canonical major.minor version "
            "at or above 3.11"
        )
    major, minor = (int(component) for component in value.split("."))
    if (major, minor) < (3, 11):
        raise AssertionError(
            f"{TEST_PYTHON_VARIABLE} must be a canonical major.minor version "
            "at or above 3.11"
        )
    return value


TEST_PYTHON = select_test_python(os.environ)

APPLICATION_ID = "me.vorotnikov.Obozrenie"
EXPECTED_DEPENDENCIES = [
    "xmltodict",
    "maxminddb",
    "pyxdg",
    "tomli-w",
    "requests",
    "urllib3>=2.3",
    "pygobject",
    "pycairo",
    "icmplib>=3.0.4",
    "filelock>=3.32.5",
]
EXPECTED_CLASSIFIERS = [
    "Topic :: Games/Entertainment",
    "Topic :: Internet :: WWW/HTTP",
    "Topic :: Utilities",
    "Programming Language :: Python :: 3",
]
EXPECTED_PACKAGE_DATA = {
    "obozrenie": [
        "data/applications/*.desktop",
        "data/icons/games/*",
        "data/icons/hicolor/scalable/apps/*.svg",
        "data/locale/*.pot",
        "data/locale/*/LC_MESSAGES/*.po",
        "data/locale/*/LC_MESSAGES/*.mo",
        "data/settings/defaults/*.toml",
        "data/settings/internal/*.toml",
        "data/settings/internal/backends/*.toml",
        "data/ui/*.ui",
    ],
}
EXPECTED_DATA_FILES = {
    "share/applications": [
        f"obozrenie/data/applications/{APPLICATION_ID}.desktop",
    ],
    "share/icons/hicolor/scalable/apps": [
        "obozrenie/data/icons/hicolor/scalable/apps/obozrenie.svg",
        "obozrenie/data/icons/hicolor/scalable/apps/obozrenie-short.svg",
    ],
}
# Every operating-system integration file the wheel carries under its
# `.data/data` tree is a verbatim copy of one canonical package resource, so
# both the expected member set and the expected bytes follow the declared
# `data-files` boundary instead of a second hand-maintained list.
EXPECTED_OS_DATA_COPIES = {
    f"{directory}/{PurePosixPath(source).name}": source
    for directory, sources in EXPECTED_DATA_FILES.items()
    for source in sources
}
EXPECTED_WHEEL_DATA_FILES = set(EXPECTED_OS_DATA_COPIES)
WHEEL_DATA_ROOT = "obozrenie-1.data/data"
WHEEL_DIST_INFO = "obozrenie-1.dist-info"
WHEEL_DIST_INFO_FILES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/COPYING",
    "top_level.txt",
}
WHEEL_RECORD_PATH = f"{WHEEL_DIST_INFO}/RECORD"
EXPECTED_GAME_ICON_FILES = {
    "alienarena.png",
    "csgo.png",
    "cstrike.png",
    "dod.png",
    "doom3.png",
    "et.png",
    "freeminer.svg",
    "garrysmod.png",
    "gesource.png",
    "hl1mp.png",
    "hl2mp.png",
    "image-missing.png",
    "jediacademy.png",
    "jedioutcast.png",
    "left4dead2.png",
    "minetest.svg",
    "openarena.png",
    "openttd.png",
    "portal2.png",
    "q2.png",
    "q3a.png",
    "q4.png",
    "qw.png",
    "rigsofrods.png",
    "rtcw.png",
    "stef1.png",
    "tf.png",
    "turtlearena.png",
    "unvanquished.png",
    "urbanterror.png",
    "warsow.png",
    "wop.png",
    "xonotic.png",
}
RUNTIME_TEST_FILES = {
    "test_adapters.py",
    "test_core_queries.py",
    "test_diagnostics.py",
    "test_geoip.py",
    "test_gtk_behavior.py",
    "test_i18n.py",
    "test_launch.py",
    "test_models.py",
    "test_network_calls.py",
    "test_networking.py",
    "test_resources.py",
    "test_settings.py",
    "test_transport.py",
    "tests.py",
}
SOURCE_ONLY_TEST_FILES = {"test_delivery.py", "test_packaging.py"}
CHECKOUT_ROOT_SOURCE_FILES = {"COPYING", "README.md", "pyproject.toml"}
SDIST_ROOT = "obozrenie-1"
SDIST_GENERATED_ROOT_FILES = {"PKG-INFO", "setup.cfg"}
SDIST_ROOT_FILES = CHECKOUT_ROOT_SOURCE_FILES | SDIST_GENERATED_ROOT_FILES
SDIST_GENERATED_ROOT_MEMBERS = {
    f"{SDIST_ROOT}/{name}"
    for name in SDIST_GENERATED_ROOT_FILES
}
SDIST_EGG_INFO_PREFIX = f"{SDIST_ROOT}/obozrenie.egg-info/"
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}


def relative_files(root):
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def canonical_data_files():
    return relative_files(PROJECT_ROOT / "obozrenie" / "data")


def canonical_python_modules():
    package_root = PROJECT_ROOT / "obozrenie"
    return {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in package_root.rglob("*.py")
        if path.is_file()
    }


def canonical_package_source_paths():
    """Checkout-relative paths of every file the package tree ships."""
    return canonical_python_modules() | {
        f"obozrenie/data/{path}"
        for path in canonical_data_files()
    }


def canonical_test_source_paths():
    """Checkout-relative paths of every classified test module."""
    return {
        f"tests/{name}"
        for name in RUNTIME_TEST_FILES | SOURCE_ONLY_TEST_FILES
    }


def expected_wheel_manifest():
    return (
        canonical_package_source_paths()
        | {
            f"{WHEEL_DATA_ROOT}/{path}"
            for path in EXPECTED_WHEEL_DATA_FILES
        }
        | {
            f"{WHEEL_DIST_INFO}/{path}"
            for path in WHEEL_DIST_INFO_FILES
        }
    )


def expected_sdist_file_manifest():
    return (
        SDIST_ROOT_FILES
        | canonical_package_source_paths()
        | canonical_test_source_paths()
        | {
            f"obozrenie.egg-info/{path}"
            for path in SDIST_EGG_INFO_FILES
        }
    )


def canonical_checkout_source_bytes():
    """Read every checkout file an artifact must ship byte for byte.

    Package modules, packaged data, classified tests, and the shipped root
    sources are read from the working tree, so the artifact contract is the
    current checkout rather than a historical inventory.
    """
    categories = (
        ("package", canonical_package_source_paths()),
        ("test", canonical_test_source_paths()),
        ("root source", CHECKOUT_ROOT_SOURCE_FILES),
    )
    checkout = {}
    for label, paths in categories:
        if not paths:
            raise AssertionError(f"canonical checkout {label} inventory is empty")
        for path in paths:
            checkout[path] = (PROJECT_ROOT / path).read_bytes()
    return checkout


def require_checkout_source(checkout, path):
    """Look up one canonical checkout payload, naming an absent path."""
    try:
        return checkout[path]
    except KeyError:
        raise AssertionError(
            f"no canonical checkout source for {path}"
        ) from None


def require_expected_members(expected, manifest, *, label):
    """Every source-identity path must also be a declared archive member."""
    unknown = sorted(set(expected) - manifest)
    if unknown:
        raise AssertionError(
            f"{label} source map covers unknown members: {unknown!r}"
        )
    return expected


def expected_wheel_source_bytes(checkout):
    """Map every wheel member that must equal canonical checkout bytes."""
    expected = {
        path: require_checkout_source(checkout, path)
        for path in canonical_package_source_paths()
    }
    expected.update({
        f"{WHEEL_DATA_ROOT}/{copy_path}": require_checkout_source(
            checkout,
            source,
        )
        for copy_path, source in EXPECTED_OS_DATA_COPIES.items()
    })
    return require_expected_members(
        expected,
        expected_wheel_manifest(),
        label="wheel",
    )


def expected_sdist_source_bytes(checkout):
    """Map every sdist member that must equal canonical checkout bytes."""
    expected = {
        f"{SDIST_ROOT}/{path}": payload
        for path, payload in checkout.items()
    }
    return require_expected_members(
        expected,
        {f"{SDIST_ROOT}/{path}" for path in expected_sdist_file_manifest()},
        label="sdist",
    )


def assert_source_bytes(actual, expected, *, label):
    """Compare archive payloads against canonical checkout bytes."""
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise AssertionError(f"{label} is missing source member {missing[0]}")
    unexpected = sorted(set(actual) - set(expected))
    if unexpected:
        raise AssertionError(
            f"{label} carries unexpected source member {unexpected[0]}"
        )
    for path in sorted(expected):
        shipped, canonical = actual[path], expected[path]
        if shipped != canonical:
            raise AssertionError(
                f"{label} source mismatch for {path}: shipped "
                f"{len(shipped)} bytes (sha256 "
                f"{hashlib.sha256(shipped).hexdigest()}) differ from the "
                f"checkout's {len(canonical)} bytes (sha256 "
                f"{hashlib.sha256(canonical).hexdigest()})"
            )


def is_sdist_generated_member(name):
    """Generated sdist metadata is checked semantically, not byte for byte."""
    return (
        name in SDIST_GENERATED_ROOT_MEMBERS
        or name.startswith(SDIST_EGG_INFO_PREFIX)
    )


def assert_wheel_source_identity(archive, checkout):
    """Every wheel member outside dist-info must equal its checkout bytes.

    The whole source region is collected, never just the members the
    checkout still has, so a file deleted from the checkout after the build
    is reported as an unexpected member instead of being skipped.
    """
    actual = {
        name: archive.read(name)
        for name in archive.namelist()
        if not name.startswith(f"{WHEEL_DIST_INFO}/")
    }
    assert_source_bytes(
        actual,
        expected_wheel_source_bytes(checkout),
        label="wheel",
    )


def carries_sdist_bytes(member):
    """Report whether this member has bytes to compare, or reject it.

    A directory has none and is simply passed over. A symbolic or hard link,
    a device node, or a FIFO has none either, but passing one over would let
    it through every byte-for-byte check untouched — into an extraction, in
    the runtime suite's case — so it ends the check instead.
    """
    if member.isdir():
        return False
    if not member.isfile():
        raise AssertionError(
            f"unsupported tar member type: {member.name!r} ({member.type!r})"
        )
    return True


def assert_sdist_source_identity(archive, checkout):
    """Every sdist member outside generated metadata equals checkout bytes."""
    actual = {}
    for member in archive.getmembers():
        if not carries_sdist_bytes(member):
            continue
        if is_sdist_generated_member(member.name):
            continue
        stream = archive.extractfile(member)
        if stream is None:
            raise AssertionError(f"cannot read sdist member {member.name}")
        actual[member.name] = stream.read()
    assert_source_bytes(
        actual,
        expected_sdist_source_bytes(checkout),
        label="sdist",
    )


def require_equal(actual, expected, label):
    """Raise the one failure a generated-metadata check reports."""
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_wheel_dist_info_members(archive):
    """The dist-info region holds exactly the members the build emits."""
    prefix = f"{WHEEL_DIST_INFO}/"
    require_equal(
        sorted(
            name.removeprefix(prefix)
            for name in archive.namelist()
            if name.startswith(prefix)
        ),
        sorted(WHEEL_DIST_INFO_FILES),
        "wheel dist-info members",
    )


def assert_distribution_metadata(payload, label):
    """This project, this version, and these dependencies, exactly.

    The wheel's `METADATA` and the sdist's `PKG-INFO` declare the same core
    fields, so both are read here.
    """
    metadata = BytesParser(policy=email.policy.default).parsebytes(payload)
    version = tuple(
        int(component) for component in metadata["Metadata-Version"].split(".")
    )
    if version < (2, 4):
        raise AssertionError(f"{label} Metadata-Version too old: {version!r}")
    require_equal(metadata["Name"], "obozrenie", f"{label} Name")
    require_equal(metadata["Version"], "1", f"{label} Version")
    require_equal(
        metadata["Requires-Python"],
        ">=3.11",
        f"{label} Requires-Python",
    )
    require_equal(
        metadata["License-Expression"],
        "GPL-3.0-only",
        f"{label} License-Expression",
    )
    require_equal(
        metadata.get_all("License-File"),
        ["COPYING"],
        f"{label} License-File",
    )
    require_equal(metadata["License"], None, f"{label} License")
    require_equal(
        metadata.get_all("Classifier"),
        EXPECTED_CLASSIFIERS,
        f"{label} Classifier",
    )
    require_equal(
        metadata.get_all("Requires-Dist"),
        EXPECTED_DEPENDENCIES,
        f"{label} Requires-Dist",
    )


def assert_wheel_entry_points(archive):
    """entry_points.txt declares exactly the console script this ships."""
    entry_points = configparser.ConfigParser(interpolation=None)
    entry_points.read_string(
        archive.read(f"{WHEEL_DIST_INFO}/entry_points.txt").decode("utf-8")
    )
    require_equal(
        {
            section: dict(entry_points[section])
            for section in entry_points.sections()
        },
        {"console_scripts": {"obozrenie-gtk": "obozrenie.launch_gtk:main"}},
        "wheel entry points",
    )


def assert_wheel_format_metadata(archive):
    """WHEEL declares a pure-Python wheel of the one tag this project ships."""
    wheel = BytesParser(policy=email.policy.default).parsebytes(
        archive.read(f"{WHEEL_DIST_INFO}/WHEEL")
    )
    require_equal(
        wheel.get_all("Wheel-Version"),
        ["1.0"],
        "wheel WHEEL Wheel-Version",
    )
    require_equal(
        wheel.get_all("Root-Is-Purelib"),
        ["true"],
        "wheel WHEEL Root-Is-Purelib",
    )
    require_equal(wheel.get_all("Tag"), ["py3-none-any"], "wheel WHEEL Tag")
    generators = wheel.get_all("Generator") or ()
    if len(generators) != 1 or not generators[0].startswith("setuptools"):
        raise AssertionError(f"unexpected wheel Generator: {generators!r}")


def assert_wheel_generated_metadata(archive):
    """Every semantic check of the metadata a build generates, in one place.

    The source region is compared byte for byte against the checkout, so
    nothing but this covers `dist-info`: without it a wheel whose
    `entry_points.txt` names another module — with a regenerated, internally
    valid `RECORD` — is a stale pair no gate would notice on the jobs where
    the installed-artifact tests are the only ones that run.
    """
    assert_wheel_dist_info_members(archive)
    assert_distribution_metadata(
        archive.read(f"{WHEEL_DIST_INFO}/METADATA"),
        "wheel METADATA",
    )
    assert_wheel_entry_points(archive)
    assert_wheel_format_metadata(archive)


def validate_install_artifact_sources(wheel, sdist, checkout):
    """Reject a stale pair before it can be installed or extracted."""
    with zipfile.ZipFile(wheel) as archive:
        assert_wheel_source_identity(archive, checkout)
        assert_wheel_generated_metadata(archive)
    with tarfile.open(sdist, "r:*") as archive:
        assert_sdist_source_identity(archive, checkout)


def expected_sdist_directory_manifest(file_paths):
    directories = {""}
    for path in file_paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def validate_archive_member_path(path):
    if type(path) is not str or not path or "\\" in path:
        raise AssertionError(f"unsafe archive member path: {path!r}")
    components = path.split("/")
    if (
        path.startswith("/")
        or any(component in {"", ".", ".."} for component in components)
        or PurePosixPath(path).as_posix() != path
    ):
        raise AssertionError(f"unsafe archive member path: {path!r}")
    return components


def zip_member(name, mode=stat.S_IFREG | 0o644):
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = mode << 16
    member.compress_type = zipfile.ZIP_DEFLATED
    return member


def stale_payload(path):
    """Checkout bytes carrying an inert marker, for stale-artifact mutants."""
    return (PROJECT_ROOT / path).read_bytes() + b"\n# stale payload\n"


def require_replacement_targets(replacements, names):
    """A replacement that matches no member would be an inert mutation."""
    unknown = sorted(set(replacements) - set(names))
    if unknown:
        raise AssertionError(
            f"replacement targets are not archive members: {unknown!r}"
        )


def regenerate_wheel_record(member_bytes):
    """Rebuild a wheel RECORD so a mutated wheel keeps a valid one.

    Row order follows the original RECORD, every non-RECORD member is
    rehashed and re-sized, and the RECORD self-row stays empty, so a stale
    payload cannot be caught by internal integrity alone.
    """
    original = member_bytes[WHEEL_RECORD_PATH].decode("utf-8")
    rows = list(csv.reader(io.StringIO(original, newline="")))
    if not all(rows):
        raise AssertionError("wheel RECORD contains a blank row")
    recorded = [row[0] for row in rows]
    added = sorted(set(member_bytes) - set(recorded))
    output = io.StringIO(newline="")
    writer = csv.writer(
        output,
        lineterminator="\r\n" if original.endswith("\r\n") else "\n",
    )
    for path in recorded + added:
        if path == WHEEL_RECORD_PATH:
            writer.writerow([path, "", ""])
            continue
        if path not in member_bytes:
            raise AssertionError(f"wheel RECORD names a missing member: {path}")
        data = member_bytes[path]
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()
        ).rstrip(b"=").decode("ascii")
        writer.writerow([path, f"sha256={digest}", len(data)])
    return output.getvalue().encode("utf-8")


def write_mutated_wheel(
    source_path,
    destination_path,
    *,
    replacements=None,
    extras=(),
    duplicate=None,
    regenerate_record=False,
):
    replacements = replacements or {}
    with zipfile.ZipFile(source_path) as source:
        source_members = [
            (
                copy.copy(member),
                replacements[member.filename]
                if member.filename in replacements
                else source.read(member),
            )
            for member in source.infolist()
        ]
    require_replacement_targets(
        replacements,
        (member.filename for member, _ in source_members),
    )

    if regenerate_record:
        member_bytes = {
            member.filename: data
            for member, data in (*source_members, *extras)
        }
        record = regenerate_wheel_record(member_bytes)
        source_members = [
            (member, record if member.filename == WHEEL_RECORD_PATH else data)
            for member, data in source_members
        ]

    with zipfile.ZipFile(destination_path, "w") as destination:
        for member, data in source_members:
            destination.writestr(member, data)
        for member, data in extras:
            destination.writestr(member, data)
        if duplicate is not None:
            member, data = next(
                (member, data)
                for member, data in source_members
                if member.filename == duplicate
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                destination.writestr(copy.copy(member), data)


def tar_member(name, data=b"", *, member_type=tarfile.REGTYPE, linkname=""):
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.linkname = linkname
    member.mode = 0o644 if member_type == tarfile.REGTYPE else 0o755
    member.mtime = 0
    member.size = len(data) if member_type == tarfile.REGTYPE else 0
    return member, data if member_type == tarfile.REGTYPE else None


def write_mutated_sdist(
    source,
    destination,
    *,
    replacements=None,
    extras=(),
    duplicate=None,
):
    replacements = replacements or {}
    with tarfile.open(source, "r:gz") as source_archive:
        source_members = []
        for member in source_archive.getmembers():
            member = copy.copy(member)
            data = None
            if member.isfile():
                extracted = source_archive.extractfile(member)
                if extracted is None:
                    raise AssertionError(
                        f"could not read tar member: {member.name}"
                    )
                data = extracted.read()
                if member.name in replacements:
                    data = replacements[member.name]
                    member.size = len(data)
            source_members.append((member, data))
    require_replacement_targets(
        replacements,
        (member.name for member, data in source_members if data is not None),
    )

    with tarfile.open(destination, "w:gz") as destination_archive:
        for member, data in source_members:
            destination_archive.addfile(
                member,
                io.BytesIO(data) if data is not None else None,
            )
        for member, data in extras:
            destination_archive.addfile(
                member,
                io.BytesIO(data) if data is not None else None,
            )
        if duplicate is not None:
            member, data = next(
                (member, data)
                for member, data in source_members
                if member.name == duplicate
            )
            destination_archive.addfile(
                copy.copy(member),
                io.BytesIO(data) if data is not None else None,
            )


def neutral_environment(**updates):
    environment = os.environ.copy()
    for name in ("CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONOPTIMIZE": "0",
        "PYTHONUTF8": "1",
    })
    environment.update({key: str(value) for key, value in updates.items()})
    return environment


def run_command(argv, *, cwd, environment=None, timeout=300):
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=environment or neutral_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        command = " ".join(str(argument) for argument in error.cmd)
        raise AssertionError(
            f"command failed ({error.returncode}): {command}\n"
            f"stdout:\n{error.stdout}\nstderr:\n{error.stderr}"
        ) from error


def create_seeded_venv(root, name, python_version=TEST_PYTHON):
    python_version = select_test_python({
        TEST_PYTHON_VARIABLE: python_version,
    })
    venv = root / name
    run_command(
        [
            "uv",
            "venv",
            "--seed",
            "--python",
            python_version,
            str(venv),
        ],
        cwd=root,
    )
    python = venv / "bin" / "python"
    version = run_command(
        [
            str(python),
            "-c",
            "import sys; print(f'{sys.version_info.major}."
            "{sys.version_info.minor}')",
        ],
        cwd=root,
    )
    if version.stdout.strip() != python_version:
        raise AssertionError(
            f"expected Python {python_version}, got {version.stdout!r}"
        )
    return venv, python


def install_wheel(python, wheel, *, cwd):
    run_command(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=cwd,
    )


def wheel_metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise AssertionError(
                f"expected one wheel METADATA file, got {metadata_names!r}"
            )
        return BytesParser(policy=email.policy.default).parsebytes(
            archive.read(metadata_names[0])
        )


def derive_prefix_purelib(python, prefix, *, cwd):
    result = run_command(
        [
            str(python),
            "-c",
            "import os, sysconfig; "
            "prefix = os.environ['OBOZRENIE_PREFIX']; "
            "print(sysconfig.get_path('purelib', "
            "vars={'base': prefix, 'platbase': prefix}))",
        ],
        cwd=cwd,
        environment=neutral_environment(OBOZRENIE_PREFIX=prefix),
    )
    return Path(result.stdout.strip())


def extract_runtime_tests(sdist, destination):
    """Write the shipped runtime suite only once it matches the checkout."""
    checkout = canonical_checkout_source_bytes()
    with tarfile.open(sdist, "r:*") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise AssertionError("duplicate tar member name")
        validated_members = [
            (member, validate_archive_member_path(member.name))
            for member in members
        ]

        extracted = {}
        for member, parts in validated_members:
            if not carries_sdist_bytes(member):
                continue
            if (
                len(parts) != 3
                or parts[0] != SDIST_ROOT
                or parts[1] != "tests"
                or parts[2] not in RUNTIME_TEST_FILES
            ):
                continue
            source = archive.extractfile(member)
            if source is None:
                raise AssertionError(f"cannot read runtime test {member.name}")
            extracted[parts[2]] = source.read()
    if set(extracted) != RUNTIME_TEST_FILES:
        raise AssertionError(
            f"runtime test inventory mismatch: {sorted(extracted)!r}"
        )
    assert_source_bytes(
        {f"tests/{name}": data for name, data in extracted.items()},
        {
            f"tests/{name}": require_checkout_source(checkout, f"tests/{name}")
            for name in RUNTIME_TEST_FILES
        },
        label="sdist runtime test",
    )

    destination.mkdir(parents=True)
    for name, data in extracted.items():
        (destination / name).write_bytes(data)


INSTALLED_SMOKE = r'''
import importlib.metadata
import importlib.util
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import obozrenie
from obozrenie import core, game_table, geoip, gtk, i18n, models, resources
from obozrenie.models import Endpoint, Player, Server

expected_root = Path(os.environ["OBOZRENIE_EXPECTED_ROOT"]).resolve()

def assert_under_expected(path):
    resolved = Path(path).resolve()
    assert resolved.is_relative_to(expected_root), (resolved, expected_root)
    return str(resolved)

package_origin = assert_under_expected(obozrenie.__file__)
resource_module_origin = assert_under_expected(resources.__file__)
resource_origin = assert_under_expected(
    resources.resource("settings", "internal", "game_lists.toml")
)
distribution_origin = assert_under_expected(
    importlib.metadata.distribution("obozrenie").locate_file("")
)
assert importlib.util.find_spec("babel") is None

core_instance = core.Core(
    game_table.GameTable.from_shipped_list(), geoip.Geolocation()
)
games = core_instance.game_table.get_game_set()
assert "garrysmod" in games
assert core_instance.game_table.get_game_info("garrysmod")["steam_app_id"] == "4000"

translations = i18n.translation(["ru_RU"])
assert translations.gettext("Connect") == "Подключиться"

class ParsingBuilder:
    def __init__(self):
        self.documents = []

    def add_from_string(self, document):
        self.documents.append(ET.fromstring(document))

builder = ParsingBuilder()
gtk.load_builder_resources(builder, translations)
assert len(builder.documents) == 2
assert all(document.tag == "interface" for document in builder.documents)
assert all(
    "translatable" not in element.attrib
    for document in builder.documents
    for element in document.iter()
)
assert any(
    element.text == "Серверы"
    for document in builder.documents
    for element in document.iter()
)
assert any(
    element.text == "О программе"
    for document in builder.documents
    for element in document.iter()
)

class StoreOnlyBuilder(gtk.Gtk.Builder):
    """Build only the delivered list stores, never a toplevel window."""

    STORE_IDS = ("player-list-store", "server-list-store")

    def add_from_string(self, document):
        return self.add_objects_from_string(document, list(self.STORE_IDS))

store_builder = StoreOnlyBuilder()
gtk.load_builder_resources(store_builder)
player_store = store_builder.get_object("player-list-store")
server_store = store_builder.get_object("server-list-store")
assert store_builder.get_object("Main_Window") is None
assert {type(built) for built in store_builder.get_objects()} == {
    gtk.Gtk.ListStore
}

floor, ceiling = models._GINT_MIN, models._GINT_MAX
assert (floor, ceiling) == (-2147483648, 2147483647), (floor, ceiling)
for value in (floor, ceiling):
    player = Player("boundary", score=value, ping=value)
    player_store.append([player.name, player.score, player.ping])
    # Counts stay nonnegative, so only ping reaches the signed floor.
    server = Server(
        endpoint=Endpoint("boundary.example", 1),
        game_id="garrysmod",
        player_count=max(value, 0),
        player_limit=max(value, 0),
        ping=value,
    )
    # Only the record's own columns matter here; the icon and filter
    # columns the UI derives keep their in-domain defaults.
    defaults = {"gchararray": "", "gboolean": False, "gint": 0}
    row = [
        defaults.get(server_store.get_column_type(index).name)
        for index in range(server_store.get_n_columns())
    ]
    row[0] = str(server.endpoint)
    row[1] = server.password
    row[2] = server.player_count
    row[3] = server.player_limit
    row[4] = server.ping
    server_store.append(row)

assert [tuple(row)[:3] for row in player_store] == [
    ("boundary", floor, floor),
    ("boundary", ceiling, ceiling),
]
assert [tuple(row)[2:5] for row in server_store] == [
    (0, 0, floor),
    (ceiling, ceiling, ceiling),
]

print("OBOZRENIE_SMOKE=" + json.dumps({
    "distribution_origin": distribution_origin,
    "package_origin": package_origin,
    "resource_module_origin": resource_module_origin,
    "resource_origin": resource_origin,
    "pythonpath": os.environ.get("PYTHONPATH"),
}, sort_keys=True))
'''


WHEEL_ZIP_IMPORT_SMOKE = r'''
import gettext
import importlib.util
import json
import os
import sys

wheel = os.path.abspath(os.environ["OBOZRENIE_WHEEL"])
checkout = os.path.realpath(os.environ["OBOZRENIE_CHECKOUT"])

def under_checkout(path):
    if not path:
        return False
    try:
        return os.path.commonpath((checkout, os.path.realpath(path))) == checkout
    except ValueError:
        return False

assert not any(under_checkout(path) for path in sys.path), sys.path
editable_prefix = "__editable___obozrenie_"
assert not any(name.startswith(editable_prefix) for name in sys.modules)
assert not any(
    getattr(finder, "__module__", type(finder).__module__).startswith(
        editable_prefix
    )
    for finder in sys.meta_path
)
original_path = sys.path[:]
sys.path = [
    path for path in sys.path
    if os.path.abspath(path) != wheel
]
try:
    fallback_spec = importlib.util.find_spec("obozrenie")
finally:
    sys.path = original_path
assert fallback_spec is None, fallback_spec

# Only LANGUAGE is a resolvable, nonempty message-locale variable: LC_CTYPE
# is set to prove it is never consulted, and LC_ALL/LC_MESSAGES/LANG are
# lower-priority C values that would produce NullTranslations if consulted
# ahead of LANGUAGE.
assert os.environ.get("LC_CTYPE") == "C", os.environ.get("LC_CTYPE")
assert os.environ.get("LANGUAGE") == "zz_ZZ:ru_RU", os.environ.get("LANGUAGE")
assert os.environ.get("LC_ALL") == "C", os.environ.get("LC_ALL")
assert os.environ.get("LC_MESSAGES") == "C", os.environ.get("LC_MESSAGES")
assert os.environ.get("LANG") == "C", os.environ.get("LANG")

import obozrenie
from obozrenie import resources
from obozrenie import i18n
import xdg

def wheel_origin(value):
    origin = str(value)
    assert origin.startswith(wheel + os.sep), (origin, wheel)
    return origin

package_origin = wheel_origin(obozrenie.__file__)
resource_module_origin = wheel_origin(resources.__file__)
i18n_module_origin = wheel_origin(i18n.__file__)
resource_root_origin = wheel_origin(resources.ROOT)
catalog_origin = wheel_origin(resources.resource(
    "locale",
    "ru",
    "LC_MESSAGES",
    "me.vorotnikov.Obozrenie.mo",
))
dependency_root = os.path.realpath(os.environ["OBOZRENIE_DEPENDENCY_ROOT"])
xdg_origin = os.path.realpath(xdg.__file__)
assert os.path.commonpath((dependency_root, xdg_origin)) == dependency_root

assert resources.ROOT.is_dir()
assert not isinstance(resources.ROOT, os.PathLike), type(resources.ROOT)
try:
    os.fspath(resources.ROOT)
except TypeError:
    pass
else:
    raise AssertionError("wheel resource root unexpectedly implements PathLike")
assert not os.path.exists(resource_root_origin)
assert not os.path.exists(catalog_origin)

games = resources.load_toml("settings", "internal", "game_lists.toml")
assert games["garrysmod"]["steam_app_id"] == "4000"
assert type(i18n._translations) is gettext.GNUTranslations
assert i18n._.__self__ is i18n._translations
assert i18n._("Connect") == "Подключиться"
assert i18n._translations.gettext("About") == "О программе"

print("WHEEL_ZIP_IMPORT=" + json.dumps({
    "catalog_origin": catalog_origin,
    "i18n_module_origin": i18n_module_origin,
    "package_origin": package_origin,
    "pythonpath": os.environ.get("PYTHONPATH"),
    "resource_module_origin": resource_module_origin,
    "resource_root_origin": resource_root_origin,
    "resource_root_type": type(resources.ROOT).__name__,
    "xdg_origin": xdg_origin,
}, sort_keys=True))
'''


WHEEL_NO_FALLBACK_SMOKE = r'''
import importlib.util
import json
import os
import sys

wheel = os.path.abspath(os.environ["OBOZRENIE_WHEEL"])
checkout = os.path.realpath(os.environ["OBOZRENIE_CHECKOUT"])

def under_checkout(path):
    if not path:
        return False
    try:
        return os.path.commonpath((checkout, os.path.realpath(path))) == checkout
    except ValueError:
        return False

assert not any(under_checkout(path) for path in sys.path), sys.path
editable_prefix = "__editable___obozrenie_"
assert not any(name.startswith(editable_prefix) for name in sys.modules)
assert not any(
    getattr(finder, "__module__", type(finder).__module__).startswith(
        editable_prefix
    )
    for finder in sys.meta_path
)
assert importlib.util.find_spec("obozrenie") is None
try:
    import obozrenie
except ModuleNotFoundError as error:
    assert error.name == "obozrenie"
else:
    raise AssertionError(
        f"broken wheel unexpectedly imported obozrenie from {obozrenie.__file__}"
    )
print("WHEEL_NO_FALLBACK=" + json.dumps({
    "pythonpath": os.environ.get("PYTHONPATH"),
    "sys_path": sys.path,
}, sort_keys=True))
'''


def run_installed_smoke(
    python,
    *,
    expected_root,
    cwd,
    pythonpath=None,
):
    updates = {"OBOZRENIE_EXPECTED_ROOT": expected_root}
    if pythonpath is not None:
        updates["PYTHONPATH"] = pythonpath
    result = run_command(
        [str(python), "-c", INSTALLED_SMOKE],
        cwd=cwd,
        environment=neutral_environment(**updates),
        timeout=120,
    )
    marker = "OBOZRENIE_SMOKE="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise AssertionError(
            f"smoke did not emit one origin record\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(lines[0].removeprefix(marker))


INSTALLED_RUNTIME_DISCOVERY = r'''
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

import obozrenie

expected_root = Path(os.environ["OBOZRENIE_EXPECTED_ROOT"]).resolve()
package_origin = Path(obozrenie.__file__).resolve()
assert package_origin.is_relative_to(expected_root), (package_origin, expected_root)
assert importlib.util.find_spec("babel") is None

suite = unittest.defaultTestLoader.discover(
    os.environ["OBOZRENIE_RUNTIME_TESTS"],
    pattern="test*.py",
)
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
print("OBOZRENIE_RUNTIME_RESULT=" + json.dumps({
    "errors": len(result.errors),
    "failures": len(result.failures),
    "package_origin": str(package_origin),
    "skipped": len(result.skipped),
    "tests_run": result.testsRun,
}, sort_keys=True))
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


def run_installed_runtime_discovery(
    python,
    *,
    expected_root,
    test_directory,
    cwd,
):
    result = run_command(
        [str(python), "-c", INSTALLED_RUNTIME_DISCOVERY],
        cwd=cwd,
        environment=neutral_environment(
            OBOZRENIE_EXPECTED_ROOT=expected_root,
            OBOZRENIE_RUNTIME_TESTS=test_directory,
        ),
        timeout=300,
    )
    marker = "OBOZRENIE_RUNTIME_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise AssertionError(
            f"runtime discovery did not emit one result record\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(lines[0].removeprefix(marker))


class PyprojectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            cls.configuration = tomllib.load(project_file)
        cls.project = cls.configuration["project"]
        cls.setuptools = cls.configuration["tool"]["setuptools"]

    def test_build_backend_requires_pep639_capable_setuptools(self):
        self.assertEqual(
            self.configuration["build-system"],
            {
                "requires": ["setuptools>=77"],
                "build-backend": "setuptools.build_meta",
            },
        )

    def test_license_uses_an_spdx_expression(self):
        self.assertEqual(self.project["license"], "GPL-3.0-only")

    def test_license_file_is_declared_explicitly(self):
        self.assertEqual(self.project.get("license-files"), ["COPYING"])

    def test_repository_artwork_has_one_gpl_only_license_surface(self):
        """All shipped project artwork and metadata expose only GPL-3.0-only.

        The previously bundled proprietary typeface and its ambiguous
        non-GPL attribution are gone: the orphaned root SVG is deleted, and
        no tracked text names that typeface, its author, or its license
        anymore. The three removed terms are assembled from split literals
        below so that asserting their absence does not itself trip the
        repository-wide grep gate this test enforces.
        """
        self.assertEqual(self.project["license"], "GPL-3.0-only")
        self.assertEqual(self.project.get("license-files"), ["COPYING"])
        self.assertFalse(
            (PROJECT_ROOT / "obozrenie-original.svg").exists(),
            "the orphaned pre-packaging root SVG must be deleted",
        )

        removed_typeface = "Neo" + "metric"
        removed_author = "Andres" + " Sanchez"
        removed_license = "CC-BY-SA" + "-NC"
        pattern = "|".join((removed_typeface, removed_author, removed_license))

        search = subprocess.run(
            ["git", "grep", "-n", "-E", pattern, "--", "."],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            search.returncode,
            1,
            "ambiguous attribution remains in tracked text:\n"
            f"{search.stdout}{search.stderr}",
        )

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("GPL-3.0-only", readme)
        self.assertNotIn(removed_typeface, readme)
        self.assertNotIn(removed_license, readme)

    def test_classifiers_exclude_the_deprecated_license_trove(self):
        self.assertEqual(self.project["classifiers"], EXPECTED_CLASSIFIERS)

    def test_python_runtime_dependencies_and_dev_tools_are_exact(self):
        self.assertEqual(self.project["requires-python"], ">=3.11")
        self.assertEqual(self.project["dependencies"], EXPECTED_DEPENDENCIES)
        self.assertEqual(
            self.configuration["dependency-groups"],
            {"dev": ["babel==2.18.0", "pyyaml"]},
        )

    def test_console_entry_point_is_declared(self):
        self.assertEqual(
            self.project["scripts"],
            {"obozrenie-gtk": "obozrenie.launch_gtk:main"},
        )

    def test_setuptools_package_and_os_data_boundaries_are_exact(self):
        self.assertEqual(
            self.setuptools["packages"],
            ["obozrenie", "obozrenie.adapters"],
        )
        self.assertIs(self.setuptools["include-package-data"], False)
        self.assertEqual(
            self.setuptools["package-data"],
            EXPECTED_PACKAGE_DATA,
        )
        self.assertEqual(self.setuptools["data-files"], EXPECTED_DATA_FILES)

    def test_canonical_source_data_inventory_is_exact(self):
        expected = {
            f"applications/{APPLICATION_ID}.desktop",
            "icons/hicolor/scalable/apps/obozrenie-short.svg",
            "icons/hicolor/scalable/apps/obozrenie.svg",
            f"locale/{APPLICATION_ID}.pot",
            f"locale/ru/LC_MESSAGES/{APPLICATION_ID}.mo",
            f"locale/ru/LC_MESSAGES/{APPLICATION_ID}.po",
            "settings/defaults/games.toml",
            "settings/defaults/settings.toml",
            "settings/internal/backends/qstat.toml",
            "settings/internal/game_lists.toml",
            "ui/obozrenie_gtk.ui",
            "ui/obozrenie_gtk_appmenu.ui",
        }
        expected.update(
            f"icons/games/{filename}" for filename in EXPECTED_GAME_ICON_FILES
        )
        self.assertEqual(canonical_data_files(), expected)

    def test_source_has_one_canonical_resource_tree(self):
        self.assertFalse((PROJECT_ROOT / "assets").exists())
        self.assertFalse((PROJECT_ROOT / "locale").exists())
        self.assertTrue((PROJECT_ROOT / "obozrenie" / "data").is_dir())

    def test_source_and_runtime_test_modules_are_classified(self):
        discovered = {
            path.name for path in (PROJECT_ROOT / "tests").glob("test*.py")
        }
        self.assertEqual(
            discovered,
            RUNTIME_TEST_FILES | SOURCE_ONLY_TEST_FILES,
        )

    def test_pyproject_is_the_only_dependency_list(self):
        self.assertFalse((PROJECT_ROOT / "requirements.txt").exists())

    def test_uv_lock_is_ignored_and_untracked(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", "uv.lock"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            ignored.returncode,
            0,
            f"stdout:\n{ignored.stdout}\nstderr:\n{ignored.stderr}",
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", "uv.lock"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(
            tracked.returncode,
            0,
            f"uv.lock is tracked:\n{tracked.stdout}\n{tracked.stderr}",
        )


class ArtifactInputStateTests(unittest.TestCase):
    def test_environment_state_truth_table(self):
        wheel = "/tmp/obozrenie-1-py3-none-any.whl"
        sdist = "/tmp/obozrenie-1.tar.gz"
        cases = (
            ("both absent", {}, True, None),
            (
                "both valid",
                {WHEEL_VARIABLE: wheel, SDIST_VARIABLE: sdist},
                False,
                None,
            ),
            ("wheel only", {WHEEL_VARIABLE: wheel}, False, "set both"),
            ("sdist only", {SDIST_VARIABLE: sdist}, False, "set both"),
            (
                "empty wheel",
                {WHEEL_VARIABLE: "", SDIST_VARIABLE: sdist},
                False,
                "non-empty",
            ),
            (
                "empty sdist",
                {WHEEL_VARIABLE: wheel, SDIST_VARIABLE: ""},
                False,
                "non-empty",
            ),
            (
                "both empty",
                {WHEEL_VARIABLE: "", SDIST_VARIABLE: ""},
                False,
                "non-empty",
            ),
        )

        for name, environment, should_skip, error_pattern in cases:
            with self.subTest(name=name):
                state = evaluate_artifact_inputs(environment)
                self.assertIs(state.should_skip_artifact_tests, should_skip)
                if error_pattern is None:
                    state.assert_valid()
                else:
                    with self.assertRaisesRegex(
                        AssertionError,
                        error_pattern,
                    ):
                        state.assert_valid()

        configured = evaluate_artifact_inputs({
            WHEEL_VARIABLE: wheel,
            SDIST_VARIABLE: sdist,
        })
        self.assertEqual(configured.require_artifacts(), (wheel, sdist))


class TestPythonSelectorTests(unittest.TestCase):
    def test_selector_defaults_to_311_and_accepts_canonical_supported_versions(self):
        self.assertEqual(select_test_python({}), "3.11")
        for version in ("3.11", "3.12", "3.13", "3.14", "3.15", "4.0"):
            with self.subTest(version=version):
                self.assertEqual(
                    select_test_python({TEST_PYTHON_VARIABLE: version}),
                    version,
                )

    def test_selector_rejects_noncanonical_or_unsupported_values(self):
        for value in ("", "3", "3.10", "3.11.0", "03.11", "3.011", " 3.11"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    AssertionError,
                    "OBOZRENIE_TEST_PYTHON.*major.minor.*3.11",
                ):
                    select_test_python({TEST_PYTHON_VARIABLE: value})

    def test_seeded_venv_uses_selector_and_proves_actual_interpreter(self):
        completed = SimpleNamespace(stdout="", stderr="")
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            version = SimpleNamespace(stdout="3.13\n", stderr="")
            with mock.patch(
                f"{__name__}.run_command",
                side_effect=[completed, version],
            ) as mocked_run_command:
                _venv, python = create_seeded_venv(root, "venv", "3.13")

            self.assertEqual(python, root / "venv" / "bin" / "python")
            self.assertEqual(
                mocked_run_command.call_args_list[0].args[0],
                [
                    "uv",
                    "venv",
                    "--seed",
                    "--python",
                    "3.13",
                    str(root / "venv"),
                ],
            )

        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            with (
                mock.patch(
                    f"{__name__}.run_command",
                    side_effect=[
                        completed,
                        SimpleNamespace(stdout="3.12\n", stderr=""),
                    ],
                ),
                self.assertRaisesRegex(
                    AssertionError,
                    "expected Python 3.13, got '3.12",
                ),
            ):
                create_seeded_venv(root, "venv", "3.13")


class ArtifactInputTests(unittest.TestCase):
    def test_artifact_input_environment_is_valid(self):
        ARTIFACT_INPUTS.assert_valid()


class WheelContractMixin:
    def assert_no_forbidden_archive_paths(self, paths):
        for path in paths:
            parts = PurePosixPath(path).parts
            with self.subTest(path=path):
                self.assertNotIn("uv.lock", parts)
                self.assertNotIn("__pycache__", parts)
                self.assertFalse(path.endswith((".pyc", ".pyo")))
                self.assertNotIn(".pytest_cache", parts)
                self.assertNotIn(".mypy_cache", parts)
                self.assertNotIn(("share", "obozrenie"), tuple(zip(parts, parts[1:])))
                if parts:
                    self.assertNotIn(parts[0], {"assets", "locale"})

    def assert_wheel_record(self, archive, manifest):
        record_text = archive.read(WHEEL_RECORD_PATH).decode(
            "utf-8",
            errors="strict",
        )
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
        self.assertTrue(rows, "wheel RECORD is empty")
        self.assertTrue(
            all(len(row) == 3 for row in rows),
            "wheel RECORD rows must contain path, hash, and size",
        )
        record_paths = [row[0] for row in rows]
        if len(record_paths) != len(set(record_paths)):
            raise AssertionError("duplicate wheel RECORD path")
        for path in record_paths:
            validate_archive_member_path(path)
        self.assertEqual(
            set(record_paths),
            manifest,
            "wheel RECORD manifest mismatch",
        )

        for path, recorded_hash, recorded_size in rows:
            if path == WHEEL_RECORD_PATH:
                self.assertEqual(
                    (recorded_hash, recorded_size),
                    ("", ""),
                    "wheel RECORD self-entry must have empty hash and size",
                )
                continue
            data = archive.read(path)
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(data).digest()
            ).rstrip(b"=").decode("ascii")
            self.assertEqual(
                recorded_hash,
                f"sha256={digest}",
                f"RECORD hash mismatch for {path}",
            )
            self.assertEqual(
                recorded_size,
                str(len(data)),
                f"RECORD size mismatch for {path}",
            )

    def assert_wheel_contract(self, wheel):
        with zipfile.ZipFile(wheel) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise AssertionError("duplicate ZIP member name")
            for member in members:
                if member.is_dir():
                    raise AssertionError(
                        f"directory ZIP member is forbidden: {member.filename!r}"
                    )
                validate_archive_member_path(member.filename)
                member_type = stat.S_IFMT(member.external_attr >> 16)
                if member_type not in {0, stat.S_IFREG}:
                    raise AssertionError(
                        "unsupported ZIP member type: "
                        f"{member.filename!r} ({member_type:o})"
                    )

            manifest = expected_wheel_manifest()
            paths = set(names)
            self.assertEqual(
                paths,
                manifest,
                "wheel member manifest mismatch",
            )
            self.assertEqual(
                archive.read(f"{WHEEL_DIST_INFO}/licenses/COPYING"),
                (PROJECT_ROOT / "COPYING").read_bytes(),
            )
            # The same generated-metadata checks the install preflight runs,
            # so a wheel cannot pass one gate and fail the other.
            assert_wheel_generated_metadata(archive)
            # Internal RECORD integrity first, so a mutant with a regenerated
            # RECORD is still measured against the checkout itself.
            self.assert_wheel_record(archive, manifest)
            assert_wheel_source_identity(
                archive,
                canonical_checkout_source_bytes(),
            )
            self.assert_no_forbidden_archive_paths(paths)

    def assert_sdist_contract(self, sdist):
        with tarfile.open(sdist, "r:*") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise AssertionError("duplicate tar member name")

            regular_files = set()
            directories = set()
            for member in members:
                parts = validate_archive_member_path(member.name)
                if parts[0] != SDIST_ROOT:
                    raise AssertionError(
                        f"unexpected sdist archive root: {parts[0]!r}"
                    )
                relative_path = "/".join(parts[1:])
                if member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                    regular_files.add(relative_path)
                elif member.type == tarfile.DIRTYPE:
                    directories.add(relative_path)
                else:
                    raise AssertionError(
                        "unsupported tar member type: "
                        f"{member.name!r} ({member.type!r})"
                    )

            expected_files = expected_sdist_file_manifest()
            expected_directories = expected_sdist_directory_manifest(
                expected_files
            )
            self.assertEqual(
                regular_files,
                expected_files,
                "sdist member manifest mismatch for regular files",
            )
            self.assertEqual(
                directories,
                expected_directories,
                "sdist member manifest mismatch for directories",
            )

            copying = archive.extractfile(f"{SDIST_ROOT}/COPYING")
            self.assertIsNotNone(copying)
            self.assertEqual(copying.read(), (PROJECT_ROOT / "COPYING").read_bytes())
            package_info = archive.extractfile(f"{SDIST_ROOT}/PKG-INFO")
            self.assertIsNotNone(package_info)
            assert_distribution_metadata(package_info.read(), "sdist PKG-INFO")
            assert_sdist_source_identity(
                archive,
                canonical_checkout_source_bytes(),
            )
            self.assert_no_forbidden_archive_paths(
                regular_files | directories
            )


@unittest.skipIf(
    ARTIFACT_INPUTS.should_skip_artifact_tests,
    f"set both {WHEEL_VARIABLE} and {SDIST_VARIABLE} for wheel mutation tests",
)
class WheelContractMutationTests(WheelContractMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheel, _ = ARTIFACT_INPUTS.require_artifacts()
        cls.wheel = Path(wheel)

    def assert_mutant_rejected(self, message, **mutation):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-wheel-mutation-",
            dir=TEMP_ROOT,
        ) as directory:
            mutant = Path(directory) / "mutant.whl"
            write_mutated_wheel(self.wheel, mutant, **mutation)
            with self.assertRaisesRegex(AssertionError, message):
                self.assert_wheel_contract(mutant)

    def test_wheel_rejects_an_extra_root_file(self):
        self.assert_mutant_rejected(
            "wheel member manifest",
            extras=((zip_member("unexpected.txt"), b"unexpected\n"),),
        )

    def test_wheel_rejects_a_duplicate_member_name(self):
        self.assert_mutant_rejected(
            "duplicate ZIP member",
            duplicate="obozrenie/__init__.py",
        )

    def test_wheel_rejects_a_directory_member(self):
        self.assert_mutant_rejected(
            "directory ZIP member",
            extras=((zip_member("unexpected/", stat.S_IFDIR | 0o755), b""),),
        )

    def test_wheel_rejects_an_unsafe_member_path(self):
        self.assert_mutant_rejected(
            "unsafe archive member path",
            extras=((zip_member("../escape"), b"escape\n"),),
        )

    def test_wheel_rejects_a_link_member(self):
        self.assert_mutant_rejected(
            "unsupported ZIP member type",
            extras=((zip_member("unexpected-link", stat.S_IFLNK | 0o777), b"target"),),
        )

    def test_wheel_rejects_invalid_wheel_metadata(self):
        self.assert_mutant_rejected(
            "Wheel-Version",
            replacements={
                f"{WHEEL_DIST_INFO}/WHEEL": (
                    b"Wheel-Version: 9.9\n"
                    b"Generator: setuptools (999)\n"
                    b"Root-Is-Purelib: true\n"
                    b"Tag: py3-none-any\n\n"
                ),
            },
        )

    def test_wheel_rejects_a_stale_record_hash(self):
        self.assert_mutant_rejected(
            "RECORD hash",
            replacements={"obozrenie/__init__.py": b"# mutated payload\n"},
        )

    def assert_stale_member_rejected(self, member, *, source=None):
        """A member that no longer equals its checkout source is rejected."""
        self.assert_mutant_rejected(
            f"wheel source mismatch for {re.escape(member)}",
            replacements={member: stale_payload(source or member)},
            regenerate_record=True,
        )

    def test_wheel_rejects_stale_python_payload_with_valid_record(self):
        self.assert_stale_member_rejected("obozrenie/core.py")

    def test_wheel_rejects_stale_package_data_with_valid_record(self):
        self.assert_stale_member_rejected(
            "obozrenie/data/settings/internal/game_lists.toml"
        )

    def test_wheel_rejects_stale_os_copy_with_valid_record(self):
        os_copy = f"share/applications/{APPLICATION_ID}.desktop"
        self.assert_stale_member_rejected(
            f"{WHEEL_DATA_ROOT}/{os_copy}",
            source=EXPECTED_OS_DATA_COPIES[os_copy],
        )


@unittest.skipIf(
    ARTIFACT_INPUTS.should_skip_artifact_tests,
    f"set both {WHEEL_VARIABLE} and {SDIST_VARIABLE} for sdist mutation tests",
)
class SdistContractMutationTests(WheelContractMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheel, sdist = ARTIFACT_INPUTS.require_artifacts()
        cls.wheel = Path(wheel)
        cls.sdist = Path(sdist)

    def assert_mutant_rejected(self, message, **mutation):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-sdist-mutation-",
            dir=TEMP_ROOT,
        ) as directory:
            mutant = Path(directory) / "mutant.tar.gz"
            write_mutated_sdist(self.sdist, mutant, **mutation)
            with self.assertRaisesRegex(AssertionError, message):
                self.assert_sdist_contract(mutant)

    def test_sdist_rejects_an_extra_regular_file(self):
        self.assert_mutant_rejected(
            "sdist member manifest",
            extras=(tar_member("obozrenie-1/unexpected.txt", b"unexpected\n"),),
        )

    def test_sdist_rejects_a_duplicate_member_name(self):
        self.assert_mutant_rejected(
            "duplicate tar member",
            duplicate="obozrenie-1/obozrenie/__init__.py",
        )

    def test_runtime_extraction_rejects_a_duplicate_test_member(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-runtime-test-mutation-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            mutant = root / "mutant.tar.gz"
            write_mutated_sdist(
                self.sdist,
                mutant,
                duplicate="obozrenie-1/tests/test_resources.py",
            )
            with self.assertRaisesRegex(AssertionError, "duplicate tar member"):
                extract_runtime_tests(mutant, root / "runtime-tests")

    def test_sdist_rejects_an_unsafe_member_path(self):
        self.assert_mutant_rejected(
            "unsafe archive member path",
            extras=(tar_member("obozrenie-1/../escape", b"escape\n"),),
        )

    def test_sdist_rejects_an_extra_directory(self):
        self.assert_mutant_rejected(
            "sdist member manifest",
            extras=(
                tar_member(
                    "obozrenie-1/unexpected-directory",
                    member_type=tarfile.DIRTYPE,
                ),
            ),
        )

    def test_sdist_rejects_symbolic_and_hard_links(self):
        for member_type, name in (
            (tarfile.SYMTYPE, "symbolic"),
            (tarfile.LNKTYPE, "hard"),
        ):
            with self.subTest(member_type=name):
                self.assert_mutant_rejected(
                    "unsupported tar member type",
                    extras=(
                        tar_member(
                            f"obozrenie-1/unexpected-{name}-link",
                            member_type=member_type,
                            linkname="obozrenie-1/README.md",
                        ),
                    ),
                )

    def assert_stale_source_rejected(self, path):
        member = f"{SDIST_ROOT}/{path}"
        self.assert_mutant_rejected(
            f"sdist source mismatch for {re.escape(member)}",
            replacements={member: stale_payload(path)},
        )

    def test_sdist_rejects_stale_package_payload(self):
        self.assert_stale_source_rejected("obozrenie/core.py")

    def test_sdist_rejects_stale_root_source_payload(self):
        self.assert_stale_source_rejected("README.md")

    def test_sdist_rejects_stale_shipped_test_payload(self):
        self.assert_stale_source_rejected("tests/test_delivery.py")

    def test_runtime_extraction_rejects_stale_test_payload_before_writing(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-runtime-test-mutation-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            mutant = root / "mutant.tar.gz"
            write_mutated_sdist(
                self.sdist,
                mutant,
                replacements={
                    f"{SDIST_ROOT}/tests/test_resources.py": stale_payload(
                        "tests/test_resources.py"
                    ),
                },
            )
            destination = root / "runtime-tests"
            with self.assertRaisesRegex(
                AssertionError,
                "sdist runtime test source mismatch for "
                f"{re.escape('tests/test_resources.py')}",
            ):
                extract_runtime_tests(mutant, destination)
            self.assertFalse(destination.exists())

    def test_rebuilt_wheel_rejects_stale_sdist_package_payload(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-sdist-rebuild-mutation-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            mutant = root / "obozrenie-1.tar.gz"
            write_mutated_sdist(
                self.sdist,
                mutant,
                replacements={
                    f"{SDIST_ROOT}/obozrenie/core.py": stale_payload(
                        "obozrenie/core.py"
                    ),
                },
            )
            output = root / "dist"
            run_command(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--clear",
                    "--out-dir",
                    str(output),
                    str(mutant),
                ],
                cwd=root,
            )
            wheels = list(output.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with self.assertRaisesRegex(
                AssertionError,
                f"wheel source mismatch for {re.escape('obozrenie/core.py')}",
            ):
                self.assert_wheel_contract(wheels[0])

    def test_installed_preflight_rejects_matching_stale_artifacts(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-install-preflight-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            # One stale payload in both artifacts: the pair agrees with
            # itself, the wheel keeps a valid RECORD, and only the checkout
            # can tell them apart.
            stale = stale_payload("obozrenie/models.py")
            wheel = root / "obozrenie-1-py3-none-any.whl"
            sdist = root / "obozrenie-1.tar.gz"
            write_mutated_wheel(
                self.wheel,
                wheel,
                replacements={"obozrenie/models.py": stale},
                regenerate_record=True,
            )
            write_mutated_sdist(
                self.sdist,
                sdist,
                replacements={f"{SDIST_ROOT}/obozrenie/models.py": stale},
            )
            with zipfile.ZipFile(wheel) as archive:
                self.assert_wheel_record(archive, expected_wheel_manifest())
            with self.assertRaisesRegex(
                AssertionError,
                f"wheel source mismatch for {re.escape('obozrenie/models.py')}",
            ):
                validate_install_artifact_sources(
                    wheel,
                    sdist,
                    canonical_checkout_source_bytes(),
                )

    def test_installed_preflight_rejects_rewritten_generated_metadata(self):
        # Only the source region is compared against the checkout, so a wheel
        # whose `entry_points.txt` names another module — with an extra
        # dist-info member and a regenerated, internally valid RECORD — used
        # to reach the install jobs where this preflight is the only gate.
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-install-preflight-metadata-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            checkout = canonical_checkout_source_bytes()
            entry_points = f"{WHEEL_DIST_INFO}/entry_points.txt"
            with zipfile.ZipFile(self.wheel) as archive:
                original_metadata = archive.read(f"{WHEEL_DIST_INFO}/METADATA")
            self.assertIn(b"Requires-Dist: requests\n", original_metadata)
            cases = (
                (
                    "wheel entry points",
                    {
                        "replacements": {
                            entry_points: (
                                b"[console_scripts]\n"
                                b"obozrenie-gtk = attacker.module:main\n"
                            ),
                        },
                    },
                ),
                (
                    "wheel dist-info members",
                    {
                        "extras": (
                            (
                                zip_member(f"{WHEEL_DIST_INFO}/EXTRA"),
                                b"unexpected\n",
                            ),
                        ),
                    },
                ),
                (
                    "wheel METADATA Requires-Dist",
                    {
                        "replacements": {
                            f"{WHEEL_DIST_INFO}/METADATA": (
                                original_metadata.replace(
                                    b"Requires-Dist: requests\n",
                                    b"Requires-Dist: attacker-package\n",
                                )
                            ),
                        },
                    },
                ),
            )
            for index, (message, mutation) in enumerate(cases):
                with self.subTest(message=message):
                    wheel = root / f"mutant-{index}-py3-none-any.whl"
                    write_mutated_wheel(
                        self.wheel,
                        wheel,
                        regenerate_record=True,
                        **mutation,
                    )
                    # The mutant is internally consistent: only the checkout
                    # and these semantic checks can tell it apart.
                    with zipfile.ZipFile(wheel) as archive:
                        self.assert_wheel_record(
                            archive,
                            set(archive.namelist()),
                        )
                    with self.assertRaisesRegex(AssertionError, message):
                        validate_install_artifact_sources(
                            wheel,
                            self.sdist,
                            checkout,
                        )

    def test_installed_preflight_rejects_a_symlink_sdist_member(self):
        # A link carries no bytes to compare, so skipping it would let one
        # through the preflight and into the runtime extraction untouched.
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-install-preflight-link-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            sdist = root / "obozrenie-1.tar.gz"
            write_mutated_sdist(
                self.sdist,
                sdist,
                extras=(
                    tar_member(
                        f"{SDIST_ROOT}/tests/link_to_readme",
                        member_type=tarfile.SYMTYPE,
                        linkname=f"{SDIST_ROOT}/README.md",
                    ),
                ),
            )
            message = "unsupported tar member type"
            with self.assertRaisesRegex(AssertionError, message):
                validate_install_artifact_sources(
                    self.wheel,
                    sdist,
                    canonical_checkout_source_bytes(),
                )
            destination = root / "runtime-tests"
            with self.assertRaisesRegex(AssertionError, message):
                extract_runtime_tests(sdist, destination)
            self.assertFalse(destination.exists())

    def test_installed_preflight_rejects_an_extra_source_member(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-install-preflight-extra-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            # A module deleted from the checkout after the build survives in
            # both artifacts, which agree with each other and carry a valid
            # regenerated RECORD for the member they actually hold.
            member = "obozrenie/removed_module.py"
            payload = b"# deleted from the checkout\n"
            wheel = root / "obozrenie-1-py3-none-any.whl"
            sdist = root / "obozrenie-1.tar.gz"
            write_mutated_wheel(
                self.wheel,
                wheel,
                extras=((zip_member(member), payload),),
                regenerate_record=True,
            )
            write_mutated_sdist(
                self.sdist,
                sdist,
                extras=(tar_member(f"{SDIST_ROOT}/{member}", payload),),
            )
            with zipfile.ZipFile(wheel) as archive:
                self.assert_wheel_record(
                    archive,
                    expected_wheel_manifest() | {member},
                )
            checkout = canonical_checkout_source_bytes()
            with (
                tarfile.open(sdist, "r:*") as archive,
                self.assertRaisesRegex(
                    AssertionError,
                    "sdist carries unexpected source member "
                    f"{re.escape(f'{SDIST_ROOT}/{member}')}",
                ),
            ):
                assert_sdist_source_identity(archive, checkout)
            with self.assertRaisesRegex(
                AssertionError,
                f"wheel carries unexpected source member {re.escape(member)}",
            ):
                validate_install_artifact_sources(wheel, sdist, checkout)


@unittest.skipIf(
    ARTIFACT_INPUTS.should_skip_artifact_tests,
    f"set both {WHEEL_VARIABLE} and {SDIST_VARIABLE} for artifact tests",
)
class ArchiveTests(WheelContractMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheel, sdist = ARTIFACT_INPUTS.require_artifacts()
        cls.wheel = Path(wheel)
        cls.sdist = Path(sdist)

    def test_inputs_are_explicit_absolute_artifact_files(self):
        self.assertTrue(self.wheel.is_absolute())
        self.assertTrue(self.sdist.is_absolute())
        self.assertTrue(self.wheel.is_file())
        self.assertTrue(self.sdist.is_file())
        self.assertEqual(self.wheel.suffix, ".whl")
        self.assertTrue(self.sdist.name.endswith(".tar.gz"))
        self.assertFalse(
            self.wheel.resolve().is_relative_to(PROJECT_ROOT.resolve())
        )
        self.assertFalse(
            self.sdist.resolve().is_relative_to(PROJECT_ROOT.resolve())
        )

    def test_supplied_wheel_has_the_complete_delivery_contract(self):
        self.assert_wheel_contract(self.wheel)

    def run_isolated_wheel_script(self, root, wheel, script, **environment_updates):
        dependency_root = root / "dependencies"
        dependency_root.mkdir()
        xdg_spec = importlib.util.find_spec("xdg")
        self.assertIsNotNone(xdg_spec)
        xdg_locations = tuple(xdg_spec.submodule_search_locations or ())
        self.assertEqual(len(xdg_locations), 1)
        shutil.copytree(
            xdg_locations[0],
            dependency_root / "xdg",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        neutral_cwd = root / "cwd"
        neutral_cwd.mkdir()
        pythonpath = os.pathsep.join((str(wheel), str(dependency_root)))
        result = run_command(
            [sys.executable, "-S", "-P", "-c", script],
            cwd=neutral_cwd,
            environment=neutral_environment(
                OBOZRENIE_CHECKOUT=PROJECT_ROOT,
                OBOZRENIE_DEPENDENCY_ROOT=dependency_root,
                OBOZRENIE_WHEEL=wheel,
                PYTHONPATH=pythonpath,
                **environment_updates,
            ),
            timeout=120,
        )
        return result, pythonpath

    def test_wheel_first_import_uses_message_locale_and_package_resources_from_zip(
        self,
    ):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-wheel-zip-import-",
            dir=TEMP_ROOT,
        ) as directory:
            result, pythonpath = self.run_isolated_wheel_script(
                Path(directory),
                self.wheel,
                WHEEL_ZIP_IMPORT_SMOKE,
                LC_CTYPE="C",
                LANGUAGE="zz_ZZ:ru_RU",
                LC_ALL="C",
                LC_MESSAGES="C",
                LANG="C",
            )

        marker = "WHEEL_ZIP_IMPORT="
        lines = [
            line for line in result.stdout.splitlines()
            if line.startswith(marker)
        ]
        self.assertEqual(lines, lines[-1:], result.stdout)
        origins = json.loads(lines[0].removeprefix(marker))
        self.assertEqual(origins["pythonpath"], pythonpath)
        for name in (
            "catalog_origin",
            "i18n_module_origin",
            "package_origin",
            "resource_module_origin",
            "resource_root_origin",
        ):
            with self.subTest(origin=name):
                self.assertTrue(
                    origins[name].startswith(str(self.wheel) + os.sep),
                    origins,
                )
        print("WHEEL_ZIP_IMPORT_ORIGINS=" + json.dumps(origins, sort_keys=True))

    def test_broken_wheel_cannot_fall_back_to_the_editable_checkout(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-wheel-no-fallback-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            broken_wheel = root / "obozrenie-broken.whl"
            with (
                zipfile.ZipFile(self.wheel) as source,
                zipfile.ZipFile(broken_wheel, "w") as destination,
            ):
                for member in source.infolist():
                    if member.filename.startswith("obozrenie/"):
                        continue
                    destination.writestr(copy.copy(member), source.read(member))

            result, _pythonpath = self.run_isolated_wheel_script(
                root,
                broken_wheel,
                WHEEL_NO_FALLBACK_SMOKE,
            )

        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        lines = [
            line for line in result.stdout.splitlines()
            if line.startswith("WHEEL_NO_FALLBACK=")
        ]
        self.assertEqual(len(lines), 1, result.stdout)
        print(lines[0])

    def test_sdist_has_complete_sources_metadata_and_no_stale_payloads(self):
        self.assert_sdist_contract(self.sdist)

    def test_sdist_rebuilds_a_wheel_with_the_same_contract(self):
        # Only a source distribution that already carries checkout bytes may
        # seed the rebuild the wheel contract is then measured against.
        with tarfile.open(self.sdist, "r:*") as archive:
            assert_sdist_source_identity(
                archive,
                canonical_checkout_source_bytes(),
            )
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-sdist-rebuild-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            output = root / "dist"
            build = run_command(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--clear",
                    "--out-dir",
                    str(output),
                    str(self.sdist),
                ],
                cwd=root,
            )
            self.assertNotIn(
                "Package would be ignored",
                build.stdout + build.stderr,
            )
            wheels = list(output.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            self.assert_wheel_contract(wheels[0])


@unittest.skipIf(
    ARTIFACT_INPUTS.should_skip_artifact_tests,
    f"set both {WHEEL_VARIABLE} and {SDIST_VARIABLE} for install tests",
)
class InstalledArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheel, sdist = ARTIFACT_INPUTS.require_artifacts()
        cls.wheel = Path(wheel)
        cls.sdist = Path(sdist)
        # No test in this class may build an environment around artifacts
        # whose sources are not the current checkout: a stale wheel and a
        # stale extracted suite would otherwise validate each other.
        validate_install_artifact_sources(
            cls.wheel,
            cls.sdist,
            canonical_checkout_source_bytes(),
        )

    def test_wheel_installs_and_smokes_in_a_fresh_selected_python_venv(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-wheel-install-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            neutral_cwd = root / "cwd"
            neutral_cwd.mkdir()
            venv, python = create_seeded_venv(root, "venv")
            install_wheel(python, self.wheel, cwd=neutral_cwd)
            origins = run_installed_smoke(
                python,
                expected_root=venv,
                cwd=neutral_cwd,
            )
            self.assertIsNone(origins["pythonpath"])
            runtime_tests = root / "runtime-suite" / "tests"
            extract_runtime_tests(self.sdist, runtime_tests)
            runtime_result = run_installed_runtime_discovery(
                python,
                expected_root=venv,
                test_directory=runtime_tests,
                cwd=neutral_cwd,
            )
            self.assertEqual(runtime_result["errors"], 0)
            self.assertEqual(runtime_result["failures"], 0)
            self.assertEqual(runtime_result["skipped"], 0)
            print("NORMAL_INSTALL_ORIGINS=" + json.dumps(origins, sort_keys=True))
            print(
                "INSTALLED_RUNTIME_RESULT="
                + json.dumps(runtime_result, sort_keys=True)
            )

    def test_wheel_installs_into_a_clean_custom_prefix(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="obozrenie-prefix-install-",
            dir=TEMP_ROOT,
        ) as directory:
            root = Path(directory)
            neutral_cwd = root / "cwd"
            neutral_cwd.mkdir()
            _installer, python = create_seeded_venv(root, "installer")
            requirements = wheel_metadata(self.wheel).get_all("Requires-Dist")
            run_command(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    *requirements,
                ],
                cwd=neutral_cwd,
            )
            run_command(
                [
                    str(python),
                    "-c",
                    "import importlib.metadata, importlib.util; "
                    "assert importlib.util.find_spec('obozrenie') is None; "
                    "assert all(d.metadata['Name'].lower() != 'obozrenie' "
                    "for d in importlib.metadata.distributions())",
                ],
                cwd=neutral_cwd,
            )

            prefix = root / "prefix"
            run_command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--prefix",
                    str(prefix),
                    "--ignore-installed",
                    "--no-deps",
                    str(self.wheel),
                ],
                cwd=neutral_cwd,
            )
            purelib = derive_prefix_purelib(
                python,
                prefix,
                cwd=neutral_cwd,
            )
            self.assertTrue(purelib.is_relative_to(prefix))
            self.assertTrue(purelib.is_dir())
            origins = run_installed_smoke(
                python,
                expected_root=prefix,
                cwd=neutral_cwd,
                pythonpath=purelib,
            )
            self.assertEqual(origins["pythonpath"], str(purelib))
            print("PREFIX_INSTALL_ORIGINS=" + json.dumps(origins, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
