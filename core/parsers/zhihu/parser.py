from __future__ import annotations

from typing import ClassVar

from ...config import PluginConfig
from ...data import Platform
from ...download import Downloader
from ..base import BaseParser
from .card import ZhihuCardMixin
from .content import ZhihuContentMixin
from .handlers import ZhihuHandlerMixin
from .request import ZhihuRequestMixin


class ZhihuParser(
    ZhihuHandlerMixin,
    ZhihuRequestMixin,
    ZhihuCardMixin,
    ZhihuContentMixin,
    BaseParser,
):
    platform: ClassVar[Platform] = Platform(
        name="zhihu",
        display_name="\u77e5\u4e4e",
    )
    _CARD_SUMMARY_LIMIT: ClassVar[int] = 80
    _CARD_SENTENCE_MARKERS: ClassVar[tuple[str, ...]] = (
        "\u3002",
        "\uff01",
        "\uff1f",
        "\uff1b",
        "\u2026",
        "!",
        "?",
        ";",
    )
    _MEDIA_ATTRS: ClassVar[tuple[str, ...]] = (
        "src",
        "data-src",
        "data-original",
        "data-actualsrc",
        "data-default-watermark-src",
        "poster",
        "href",
    )
    _VIDEO_URL_KEYS: ClassVar[tuple[str, ...]] = (
        "playUrl",
        "playUrlHd",
        "videoUrl",
        "src",
        "url",
        "originVideoUrl",
        "videoPlayUrl",
        "playlist",
    )
    _VIDEO_COVER_KEYS: ClassVar[tuple[str, ...]] = (
        "cover",
        "coverUrl",
        "thumbnail",
        "thumbnailUrl",
        "imageUrl",
        "poster",
    )
    _VIDEO_TITLE_KEYS: ClassVar[tuple[str, ...]] = (
        "title",
        "name",
        "description",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.zhihu
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "referer": "https://www.zhihu.com/",
                "origin": "https://www.zhihu.com",
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
        )
