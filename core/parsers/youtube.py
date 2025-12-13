import re
from pathlib import Path
from typing import ClassVar

import msgspec
from aiohttp import ClientError
from msgspec import Struct

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..download import Downloader
from .base import BaseParser, Platform, handle


class YouTubeParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="youtube", display_name="油管")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.ytb_cookies_file = Path(self.config["ytb_cookies_file"]) or None
        self.max_duration = config["max_duration"]

    @handle("youtu.be", r"https?://(?:www\.)?youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+")
    @handle(
        "youtube.com",
        r"https?://(?:www\.)?youtube\.com/(?:watch|shorts)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+)",
    )
    async def _parse_video(self, searched: re.Match[str]):
        return await self.parse_video(searched)

    async def parse_video(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = searched.group(0)

        video_info = await self.downloader.ytdlp_extract_info(
            url, cookiefile=self.ytb_cookies_file
        )
        author = await self._fetch_author_info(video_info.channel_id)

        contents = []
        if video_info.duration <= self.max_duration:
            video = self.downloader.download_video(
                url, use_ytdlp=True, cookiefile=self.ytb_cookies_file
            )
            contents.append(
                self.create_video_content(
                    video,
                    video_info.thumbnail,
                    video_info.duration,
                )
            )
        else:
            contents.extend(self.create_image_contents([video_info.thumbnail]))

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
        )

    async def parse_audio(self, url: str):
        """解析 YouTube URL 并标记为音频下载

        Args:
            url: YouTube 链接

        Returns:
            ParseResult: 解析结果（音频内容）

        """
        video_info = await self.downloader.ytdlp_extract_info(url, self.ytb_cookies_file)
        author = await self._fetch_author_info(video_info.channel_id)

        contents = []
        contents.extend(self.create_image_contents([video_info.thumbnail]))

        if video_info.duration <= self.max_duration:
            audio_task = self.downloader.download_audio(
                url, use_ytdlp=True, cookiefile=self.ytb_cookies_file
            )
            contents.append(self.create_audio_content(audio_task, duration=video_info.duration))

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
        )

    async def _fetch_author_info(self, channel_id: str):
        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        payload = {
            "context": {
                "client": {
                    "hl": "zh-HK",
                    "gl": "US",
                    "deviceMake": "Apple",
                    "deviceModel": "",
                    "clientName": "WEB",
                    "clientVersion": "2.20251002.00.00",
                    "osName": "Macintosh",
                    "osVersion": "10_15_7",
                },
                "user": {"lockedSafetyMode": False},
                "request": {
                    "useSsl": True,
                    "internalExperimentFlags": [],
                    "consistencyTokenJars": [],
                },
            },
            "browseId": channel_id,
        }
        async with self.client.post(
            url,
            json=payload,
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"YouTube browse API {resp.status} {resp.reason}")
            browse = msgspec.json.decode(await resp.read(), type=BrowseResponse)

        return self.create_author(browse.name, browse.avatar_url, browse.description)





class Thumbnail(Struct):
    url: str


class AvatarInfo(Struct):
    thumbnails: list[Thumbnail]


class ChannelMetadataRenderer(Struct):
    title: str
    description: str
    avatar: AvatarInfo


class Metadata(Struct):
    channelMetadataRenderer: ChannelMetadataRenderer


class Avatar(Struct):
    thumbnails: list[Thumbnail]


class BrowseResponse(Struct):
    metadata: Metadata

    @property
    def name(self) -> str:
        return self.metadata.channelMetadataRenderer.title

    @property
    def avatar_url(self) -> str | None:
        thumbnails = self.metadata.channelMetadataRenderer.avatar.thumbnails
        return thumbnails[0].url if thumbnails else None

    @property
    def description(self) -> str:
        return self.metadata.channelMetadataRenderer.description
