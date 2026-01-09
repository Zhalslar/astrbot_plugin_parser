import asyncio
import re
from pathlib import Path
from typing import Any, ClassVar

import yt_dlp

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform
from ..download import Downloader
from ..exception import ParseException
from ..utils import save_cookies_with_netscape
from .base import BaseParser, handle


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="instagram", display_name="Instagram")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Origin": "https://www.instagram.com",
                "Referer": "https://www.instagram.com/",
            }
        )
        self._cookies_file = self._init_cookies()

    def _init_cookies(self) -> Path | None:
        ig_ck = self.config.get("ig_ck", "")
        if not ig_ck:
            return None
        cookies_file = self.data_dir / "ig_cookies.txt"
        cookies_file.parent.mkdir(parents=True, exist_ok=True)
        save_cookies_with_netscape(ig_ck, cookies_file, "instagram.com")
        return cookies_file

    async def _extract_info(self, url: str) -> dict[str, Any]:
        opts: dict[str, Any] = {"quiet": True, "skip_download": True}
        if self.proxy:
            opts["proxy"] = self.proxy
        if self._cookies_file and self._cookies_file.is_file():
            opts["cookiefile"] = str(self._cookies_file)
        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
        if not isinstance(raw, dict):
            raise ParseException("获取视频信息失败")
        return raw

    @staticmethod
    def _iter_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return [e for e in entries if isinstance(e, dict)]
        return [info]

    @staticmethod
    def _pick_video_url(info: dict[str, Any]) -> str | None:
        url = info.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        formats = info.get("formats") or []
        best: dict[str, Any] | None = None
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get("vcodec") == "none":
                continue
            fmt_url = fmt.get("url")
            if not fmt_url:
                continue
            if best is None:
                best = fmt
                continue
            if fmt.get("acodec") != "none" and best.get("acodec") == "none":
                best = fmt
                continue
            if (fmt.get("height") or 0) > (best.get("height") or 0):
                best = fmt
        return best.get("url") if best else None

    @handle(
        "instagram.com",
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[A-Za-z0-9._?%&=+\-/#]+",
    )
    @handle(
        "instagr.am",
        r"https?://(?:www\.)?instagr\.am/(?:p|reel|reels|tv)/[A-Za-z0-9._?%&=+\-/#]+",
    )
    async def _parse(self, searched: re.Match[str]):
        url = searched.group(0)
        final_url = await self.get_final_url(url, headers=self.headers)
        info = await self._extract_info(final_url)
        entries = self._iter_entries(info)

        contents = []
        meta_entry: dict[str, Any] | None = None
        for entry in entries:
            video_url = self._pick_video_url(entry)
            if not video_url:
                continue
            thumbnail = entry.get("thumbnail")
            duration = float(entry.get("duration") or 0)
            contents.append(
                self.create_video_content(
                    video_url,
                    thumbnail,
                    duration,
                    ext_headers=self.headers,
                )
            )
            if meta_entry is None:
                meta_entry = entry

        if not contents:
            raise ParseException("未找到可下载的视频")

        meta = meta_entry or info
        author_name = None
        for key in ("uploader", "uploader_id", "channel"):
            val = meta.get(key)
            if isinstance(val, str) and val:
                author_name = val
                break
        author = self.create_author(author_name) if author_name else None
        title = meta.get("title") or info.get("title")
        timestamp = meta.get("timestamp") or info.get("timestamp")

        return self.result(
            title=title,
            author=author,
            contents=contents,
            timestamp=timestamp,
        )
