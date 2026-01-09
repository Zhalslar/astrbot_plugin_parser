import asyncio
import re
from pathlib import Path
from typing import Any, ClassVar

import yt_dlp

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent
from ..download import Downloader
from ..exception import ParseException
from ..utils import generate_file_name, save_cookies_with_netscape
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

    async def _download_with_ytdlp(self, url: str) -> Path:
        output_path = self.downloader.cache_dir / generate_file_name(url, ".mp4")
        if output_path.exists():
            return output_path
        opts: dict[str, Any] = {
            "quiet": True,
            "outtmpl": str(output_path),
            "merge_output_format": "mp4",
            "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if self._cookies_file and self._cookies_file.is_file():
            opts["cookiefile"] = str(self._cookies_file)
        with yt_dlp.YoutubeDL(opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])
        return output_path

    @staticmethod
    def _iter_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return [e for e in entries if isinstance(e, dict)]
        return [info]

    @staticmethod
    def _format_url(fmt: dict[str, Any]) -> str | None:
        url = fmt.get("url")
        return url if isinstance(url, str) and url.startswith("http") else None

    @staticmethod
    def _has_video(fmt: dict[str, Any]) -> bool:
        return (fmt.get("vcodec") or "none") != "none"

    @staticmethod
    def _has_audio(fmt: dict[str, Any]) -> bool:
        acodec = fmt.get("acodec") or "none"
        return acodec not in ("none", "unknown")

    @staticmethod
    def _is_m4a(fmt: dict[str, Any]) -> bool:
        return fmt.get("ext") == "m4a"

    @staticmethod
    def _is_direct_format(fmt: dict[str, Any]) -> bool:
        return fmt.get("protocol") not in ("m3u8", "m3u8_native")

    @classmethod
    def _pick_formats(
        cls, info: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        formats = info.get("formats") or []
        video_fmt: dict[str, Any] | None = None
        audio_fmt: dict[str, Any] | None = None

        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if not cls._is_direct_format(fmt):
                continue
            if cls._format_url(fmt) is None:
                continue

            if cls._has_video(fmt):
                if video_fmt is None:
                    video_fmt = fmt
                    continue
                curr_height = video_fmt.get("height") or 0
                new_height = fmt.get("height") or 0
                if new_height > curr_height:
                    video_fmt = fmt
                elif new_height == curr_height:
                    if cls._has_audio(fmt) and not cls._has_audio(video_fmt):
                        video_fmt = fmt
                        continue
                    curr_tbr = video_fmt.get("tbr") or 0
                    new_tbr = fmt.get("tbr") or 0
                    if new_tbr > curr_tbr:
                        video_fmt = fmt
                continue

            if cls._has_audio(fmt):
                if audio_fmt is None:
                    audio_fmt = fmt
                    continue
                if cls._is_m4a(fmt) and not cls._is_m4a(audio_fmt):
                    audio_fmt = fmt
                    continue
                if cls._is_m4a(fmt) == cls._is_m4a(audio_fmt):
                    if (fmt.get("abr") or 0) > (audio_fmt.get("abr") or 0):
                        audio_fmt = fmt

        return video_fmt, audio_fmt

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
            video_fmt, audio_fmt = self._pick_formats(entry)
            video_url = self._format_url(video_fmt) if video_fmt else None
            audio_url = self._format_url(audio_fmt) if audio_fmt else None
            if not video_url:
                continue
            thumbnail = entry.get("thumbnail")
            duration = float(entry.get("duration") or 0)
            cover_task = None
            if thumbnail:
                cover_task = self.downloader.download_img(
                    thumbnail,
                    ext_headers=self.headers,
                    proxy=self.proxy,
                )
            if audio_url and video_fmt and not self._has_audio(video_fmt):
                output_path = self.downloader.cache_dir / generate_file_name(
                    video_url, ".mp4"
                )
                if output_path.exists():
                    video_task = output_path
                else:
                    video_task = self.downloader.download_av_and_merge(
                        video_url,
                        audio_url,
                        output_path=output_path,
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                contents.append(VideoContent(video_task, cover_task, duration))
            else:
                video_task = self.downloader.download_video(
                    video_url,
                    ext_headers=self.headers,
                    proxy=self.proxy,
                )
                contents.append(VideoContent(video_task, cover_task, duration))
            if meta_entry is None:
                meta_entry = entry

        meta = meta_entry or info
        if not contents:
            fallback_url = meta.get("webpage_url") or final_url
            if not isinstance(fallback_url, str) or not fallback_url:
                raise ParseException("未找到可下载的视频")
            thumbnail = meta.get("thumbnail")
            duration = float(meta.get("duration") or 0)
            cover_task = None
            if thumbnail:
                cover_task = self.downloader.download_img(
                    thumbnail,
                    ext_headers=self.headers,
                    proxy=self.proxy,
                )
            video_task = await self._download_with_ytdlp(fallback_url)
            contents.append(VideoContent(video_task, cover_task, duration))
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
