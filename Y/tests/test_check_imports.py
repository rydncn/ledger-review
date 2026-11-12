"""The import check (design: security boundaries): what it refuses and allows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests import SAMPLES


def check_imports() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_imports", SAMPLES.parent / "scripts" / "check_imports.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problems(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    found: list[str] = check_imports().problems(path)
    return [problem.split(": ", 1)[1] for problem in found]


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("import socket\n", "imports socket, which is not on the allowed list"),
        ("from urllib import request as r\n", "imports urllib, which is not on the allowed list"),
        ("import logging.handlers\n", "imports logging.handlers, which is not on the allowed list"),
        ("import logging.config\n", "imports logging.config, which is not on the allowed list"),
        (
            "from logging import handlers\n",
            "imports logging.handlers, which is not on the allowed list",
        ),
        ("import importlib\n", "imports importlib, which is not on the allowed list"),
        ('__import__("socket")\n', "__import__ is not allowed"),
        ('__builtins__["__import__"]("ssl")\n', "__builtins__ is not allowed"),
        ('exec("import socket")\n', "exec is not allowed"),
        ('eval("1")\n', "eval is not allowed"),
        ('getattr(x, "__import__")\n', "__import__ is not allowed"),
        ("x = 1.5\n", "a float literal; amounts are exact"),
    ],
)
def test_what_the_import_check_refuses(tmp_path: Path, source: str, problem: str) -> None:
    assert problem in problems(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        "",  # an empty module, such as an __init__.py
        "import logging\nfrom collections.abc import Iterator\nfrom typing import Literal\n",
        "from tally.importers import kind\nfrom . import money\nimport re\nx = re.compile('a')\n",
        "time.sleep(0.01)  # rules-check-ignore: seconds, not money\n",
    ],
)
def test_what_the_import_check_allows(tmp_path: Path, source: str) -> None:
    assert problems(tmp_path, source) == []
