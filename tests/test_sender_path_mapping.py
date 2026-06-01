from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def sender_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(error=lambda *args, **kwargs: None)

    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger

    components_module = types.ModuleType("astrbot.core.message.components")
    platform_module = types.ModuleType("astrbot.core.platform.astr_message_event")

    class BaseMessageComponent:
        pass

    class _Component(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            pass

    for name in (
        "File",
        "Image",
        "Node",
        "Nodes",
        "Plain",
        "Record",
        "Video",
    ):
        setattr(components_module, name, type(name, (_Component,), {}))
    components_module.BaseMessageComponent = BaseMessageComponent

    class AstrMessageEvent:
        pass

    platform_module.AstrMessageEvent = AstrMessageEvent

    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    monkeypatch.setitem(sys.modules, "astrbot.core.message.components", components_module)
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.astr_message_event", platform_module)
    monkeypatch.setitem(sys.modules, "core.config", types.ModuleType("core.config"))
    monkeypatch.setitem(sys.modules, "core.config", SimpleNamespace(PluginConfig=object))
    monkeypatch.setitem(sys.modules, "core.render", SimpleNamespace(Renderer=object))

    monkeypatch.delitem(sys.modules, "core.sender", raising=False)
    return importlib.import_module("core.sender")


def test_file_uri_uses_configured_send_path_prefix(sender_module):
    sender = sender_module.MessageSender.__new__(sender_module.MessageSender)
    sender.cfg = SimpleNamespace(
        local_media_path_prefix="/docker-file/AstrBot/data",
        send_media_path_prefix="/AstrBot/data",
    )

    uri = sender._to_file_uri(
        Path("/docker-file/AstrBot/data/plugin_data/astrbot_plugin_parser/cache/a.mp4")
    )

    assert uri == "file:///AstrBot/data/plugin_data/astrbot_plugin_parser/cache/a.mp4"


def test_file_uri_is_unchanged_without_mapping(sender_module):
    sender = sender_module.MessageSender.__new__(sender_module.MessageSender)
    sender.cfg = SimpleNamespace(
        local_media_path_prefix="",
        send_media_path_prefix="",
    )

    uri = sender._to_file_uri(Path("/tmp/a.mp4"))

    assert uri == "file:///tmp/a.mp4"
