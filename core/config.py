from __future__ import annotations

import zoneinfo
from typing import Any, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools


class ParserItem:
    __slots__ = ("_data",)

    # ===== 声明字段（IDE 全提示）=====
    __template_key: str
    enable: bool
    use_proxy: bool
    cookies: str
    video_codecs: str
    video_quality: str

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, key: str):
        try:
            return self._data[key]
        except KeyError:
            logger.error(f"[parser:{self._data.get('__template_key')}] 缺少字段: {key}")

    def __repr__(self) -> str:
        return f"<ParserItem {self._data.get('__template_key')}>"


# ============================================================
# 3. ParserConfig：平台可选，字段真实
# ============================================================


class ParserConfig:
    acfun: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xhs: ParserItem
    youtube: ParserItem

    def __init__(self, raw: list[dict[str, Any]]):
        hints = get_type_hints(self.__class__)
        seen: set[str] = set()
        for item in raw:
            key = item.get("__template_key")
            if not key:
                logger.warning("[parser] 缺少 __template_key，已跳过")
                continue

            if key not in hints:
                logger.warning(f"[parser] 未知平台: {key}")
                continue

            if key in seen:
                logger.warning(f"[parser] 平台 {key} 被重复配置，已覆盖之前的配置")

            seen.add(key)
            setattr(self, key, ParserItem(item))  # type: ignore[arg-type]

    def __getattr__(self, name: str):
        # 访问了声明过但未初始化的平台
        hints = get_type_hints(self.__class__)
        if name in hints:
            raise AttributeError(f"[parser] 平台未配置: {name}")
        raise AttributeError(name)


class TypedConfigFacade:
    """
    AstrBotConfig 属性代理
    """

    __annotations__: dict[str, type]

    def __init__(self, cfg: AstrBotConfig):
        object.__setattr__(self, "_cfg", cfg)

        hints = get_type_hints(self.__class__)
        object.__setattr__(
            self,
            "_fields",
            {k for k in hints if not k.startswith("_")},
        )

        for key in self._fields:
            if key not in cfg:
                logger.warning(f"[config] 缺少配置项: {key}")

    def __getattr__(self, key: str):
        if key in self._fields:
            return self._cfg.get(key)
        raise AttributeError(key)

    def __setattr__(self, key: str, value):
        if key in self._fields:
            self._cfg[key] = value
        else:
            object.__setattr__(self, key, value)

    def save(self) -> None:
        self._cfg.save_config()


class PluginConfig(TypedConfigFacade):
    """
    插件配置
    """

    disabled_sessions: list[str]
    arbiter: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_to_file: bool
    single_heavy_render_card: bool
    forward_threshold: int

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    emoji_cdn: str
    emoji_style: str
    clean_cron: str

    parsers_template: list[dict[str, Any]]

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)

        # ---------- 路径 ----------
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_parser")
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- Parser ----------
        self.parser = ParserConfig(self.parsers_template)
