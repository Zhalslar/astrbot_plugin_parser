from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def utils_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.utils")


def test_join_skips_none(utils_module):
    assert utils_module.join_nonempty_texts([None]) == ""
    assert utils_module.join_nonempty_texts([None, "https://b23.tv/x"]) == (
        "https://b23.tv/x"
    )


def test_join_skips_empty_and_keeps_order(utils_module):
    assert utils_module.join_nonempty_texts(["a", None, "", "b"]) == "ab"
