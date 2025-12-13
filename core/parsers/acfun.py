import asyncio
import json
import re
import time
from pathlib import Path
from typing import ClassVar

import aiofiles
from aiohttp import ClientError

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..download import Downloader
from ..exception import DownloadException, ParseException
from ..utils import safe_unlink
from .base import BaseParser, Platform, handle


class AcfunParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="acfun", display_name="A站")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers["referer"] = "https://www.acfun.cn/"
        self.cache_dir = Path(config["cache_dir"])
        self.max_size = self.config["max_size"]

    @handle("acfun.cn", r"(?:ac=|/ac)(?P<acid>\d+)")
    async def _parse(self, searched: re.Match[str]):
        acid = int(searched.group("acid"))
        url = f"https://www.acfun.cn/v/ac{acid}"

        m3u8_url, title, description, author, upload_time = await self.parse_video_info(url)
        author = self.create_author(author) if author else None

        # 2024-12-1 -> timestamp
        try:
            timestamp = int(time.mktime(time.strptime(upload_time, "%Y-%m-%d")))
        except ValueError:
            timestamp = None
        text = f"简介: {description}"

        # 下载视频
        video_task = asyncio.create_task(self.download_video(m3u8_url, acid))

        return self.result(
            title=title,
            text=text,
            author=author,
            timestamp=timestamp,
            contents=[self.create_video_content(video_task)],
        )

    async def parse_video_info(self, url: str) -> tuple[str, str, str, str, str]:
        """解析acfun链接获取详细信息

        Args:
            url (str): 链接

        Returns:
            tuple: (m3u8_url, title, description, author, upload_time)
        """

        # 拼接查询参数
        url = f"{url}?quickViewId=videoInfo_new&ajaxpipe=1"

        async with self.client.get(url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"HTTP {resp.status}")
            raw = await resp.text()

        matched = re.search(r"window\.videoInfo =(.*?)</script>", raw)
        if not matched:
            raise ParseException("解析 acfun 视频信息失败")
        json_str = str(matched.group(1))
        json_str = json_str.replace('\\\\"', '\\"').replace('\\"', '"')
        video_info = json.loads(json_str)

        title = video_info.get("title", "")
        description = video_info.get("description", "")
        author = video_info.get("user", {}).get("name", "")
        upload_time = video_info.get("createTime", "")

        ks_play_json = video_info["currentVideoInfo"]["ksPlayJson"]
        ks_play = json.loads(ks_play_json)
        representations = ks_play["adaptationSet"][0]["representation"]
        # 这里[d['url'] for d in representations]，从 4k ~ 360，此处默认720p
        m3u8_url = [d["url"] for d in representations][3]

        return m3u8_url, title, description, author, upload_time

    async def download_video(self, m3u8s_url: str, acid: int) -> Path:
        """下载acfun视频

        Args:
            m3u8s_url (str): m3u8链接
            acid (int): acid

        Returns:
            Path: 下载的mp4文件
        """

        m3u8_full_urls = await self._parse_m3u8(m3u8s_url)
        video_file = self.cache_dir / f"acfun_{acid}.mp4"
        if video_file.exists():
            return video_file

        max_size = self.max_size * 1024 * 1024

        try:
            async with aiofiles.open(video_file, "wb") as f:
                with self.downloader.get_progress_bar(video_file.name) as bar:
                    total = 0
                    for url in m3u8_full_urls:
                        async with self.client.get(url, headers=self.headers) as resp:
                            if resp.status >= 400:
                                raise ClientError(f"{resp.status} {resp.reason}")
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                await f.write(chunk)
                                total += len(chunk)
                                bar.update(len(chunk))
                                if total > max_size:        # 大小截断
                                    break
                        if total > max_size:
                            break

        except ClientError:
            await safe_unlink(video_file)
            logger.exception("视频下载失败")
            raise DownloadException("视频下载失败")
        return video_file

    async def _parse_m3u8(self, m3u8_url: str):
        """解析m3u8链接

        Args:
            m3u8_url (str): m3u8链接

        Returns:
            list[str]: 视频链接
        """
        async with self.client.get(m3u8_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"{resp.status} {resp.reason}")
            m3u8_file = await resp.text()
        # 分离ts文件链接
        raw_pieces = re.split(r"\n#EXTINF:.{8},\n", m3u8_file)
        # 过滤头部\
        m3u8_relative_links = raw_pieces[1:]

        # 修改尾部 去掉尾部多余的结束符
        patched_tail = m3u8_relative_links[-1].split("\n")[0]
        m3u8_relative_links[-1] = patched_tail

        # 完整链接，直接加 m3u8Url 的通用前缀
        m3u8_prefix = "/".join(m3u8_url.split("/")[0:-1])
        m3u8_full_urls = [f"{m3u8_prefix}/{d}" for d in m3u8_relative_links]

        return m3u8_full_urls
