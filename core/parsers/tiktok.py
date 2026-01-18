import re
from typing import ClassVar

from ..config import PluginConfig
from ..data import Author, Platform, VideoContent
from ..download import Downloader
from .base import BaseParser, handle


class TikTokParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="tiktok", display_name="TikTok")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.tiktok

    @handle("tiktok.com", r"(?:https?://)?(www|vt|vm)\.tiktok\.com/[A-Za-z0-9._?%&+\-=/#@]*")
    async def _parse(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url, prefix = searched.group(0), searched.group(1)

        if prefix in ("vt", "vm"):
            url = await self.get_redirect_url(url)

        # 获取视频信息
        video_info = await self.downloader.ytdlp_extract_info(url, headers=self.headers, proxy=self.proxy)

        # 下载封面和视频
        cover = self.downloader.download_img(video_info.thumbnail, proxy=self.proxy)
        video = self.downloader.download_video(url, use_ytdlp=True, proxy=self.proxy)

        return self.result(
            title=video_info.title,
            author=Author(name=video_info.channel),
            contents=[VideoContent(video, cover, duration=video_info.duration)],
            timestamp=video_info.timestamp,
        )
