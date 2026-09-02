import tomllib
from importlib.resources import files
from importlib.resources.abc import Traversable


ROOT = files("obozrenie").joinpath("data")


def resource(*parts: str) -> Traversable:
    item = ROOT
    for part in parts:
        item = item.joinpath(part)
    return item


def read_text(*parts: str) -> str:
    with resource(*parts).open(
        "r",
        encoding="utf-8",
        errors="strict",
    ) as handle:
        return handle.read()


def read_bytes(*parts: str) -> bytes:
    with resource(*parts).open("rb") as handle:
        return handle.read()


def load_toml(*parts: str) -> dict:
    with resource(*parts).open("rb") as handle:
        return tomllib.load(handle)
