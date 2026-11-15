"""Laedt die Skripte mit Bindestrich im Namen als Module und stellt die Fixtures bereit."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# istiocli.py liegt im Repo-Root und wird von den Skripten per "import istiocli" geladen
sys.path.insert(0, str(ROOT))


def load_script(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py").replace("-", "_"), ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hc() -> ModuleType:
    return load_script("istio-healthcheck.py")


@pytest.fixture(scope="session")
def inspect_script() -> ModuleType:
    return load_script("istio-inspect.py")


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_items(name: str) -> list:
    return json.loads(fixture_text(name))["items"]
