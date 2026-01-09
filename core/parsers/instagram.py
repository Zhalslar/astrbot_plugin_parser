import asyncio
import html
import re
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import yt_dlp

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ImageContent, Platform, VideoContent
from ..download import Downloader
from ..exception import ParseException
from ..utils import generate_file_name, safe_unlink, save_cookies_with_netscape
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

    async def _fetch_og_meta(self, url: str) -> dict[str, str]:
        async with self.client.get(
            url, headers=self.headers, allow_redirects=True, proxy=self.proxy
        ) as resp:
            if resp.status >= 400:
                raise ParseException(f"获取页面失败 {resp.status}")
            html = await resp.text()

        def _match(prop: str) -> str | None:
            pattern = rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"'
            if matched := re.search(pattern, html):
                return matched.group(1)
            return None

        og_image = _match("og:image") or ""
        og_video = _match("og:video") or ""
        return {
            "image": self._clean_url(og_image) if og_image else "",
            "video": self._clean_url(og_video) if og_video else "",
            "title": _match("og:title") or "",
        }

    async def _upgrade_image_url(
        self, image_url: str, shortcode: str | None
    ) -> str:
        if not shortcode:
            return image_url
        if "s640x640" not in image_url and "stp=" not in image_url:
            return image_url
        probe_url = f"https://www.instagram.com/p/{shortcode}/media/?size=l"
        try:
            return await self.get_final_url(probe_url, headers=self.headers)
        except Exception:
            return image_url

    async def _extract_info(self, url: str) -> dict[str, Any]:
        retries = 2
        last_exc: Exception | None = None
        opts: dict[str, Any] = {
            "quiet": True,
            "skip_download": True,
            "ignore_no_formats": True,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if self._cookies_file and self._cookies_file.is_file():
            opts["cookiefile"] = str(self._cookies_file)
        for attempt in range(retries + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
                if not isinstance(raw, dict):
                    raise ParseException("获取视频信息失败")
                return raw
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("获取视频信息失败") from exc
        raise ParseException("获取视频信息失败") from last_exc

    async def _download_with_ytdlp(
        self, url: str, output_name: str | None = None
    ) -> Path:
        if output_name:
            output_path = self.downloader.cache_dir / output_name
        else:
            output_path = self.downloader.cache_dir / generate_file_name(url, ".mp4")
        if output_path.exists():
            return output_path
        retries = 2
        last_exc: Exception | None = None
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
        for attempt in range(retries + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])
                return output_path
            except Exception as exc:
                last_exc = exc
                await safe_unlink(output_path)
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("下载失败") from exc
        raise ParseException("下载失败") from last_exc

    @staticmethod
    def _iter_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return [e for e in entries if isinstance(e, dict)]
        return [info]

    @staticmethod
    def _clean_url(url: str) -> str:
        return html.unescape(url)

    @classmethod
    def _format_url(cls, fmt: dict[str, Any]) -> str | None:
        url = fmt.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return cls._clean_url(url)
        return None

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

    @staticmethod
    def _extract_shortcode(url: str) -> str | None:
        path = urlparse(url).path
        if matched := re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)/?", path):
            return matched.group(1)
        return None

    @staticmethod
    def _entry_identity(entry: dict[str, Any], fallback: str) -> str:
        for key in ("id", "display_id", "shortcode"):
            val = entry.get(key)
            if val:
                return str(val)
        return fallback

    @staticmethod
    def _url_suffix(url: str, default: str) -> str:
        suffix = Path(urlparse(url).path).suffix
        return suffix if suffix else default

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

    @staticmethod
    def _pick_image_url(info: dict[str, Any]) -> str | None:
        url = info.get("url")
        if isinstance(url, str) and url.startswith("http"):
            ext = info.get("ext")
            if ext in ("jpg", "jpeg", "png", "webp"):
                return InstagramParser._clean_url(url)
        thumbnails = info.get("thumbnails") or []
        best: dict[str, Any] | None = None
        for thumb in thumbnails:
            if not isinstance(thumb, dict):
                continue
            t_url = thumb.get("url")
            if not isinstance(t_url, str) or not t_url.startswith("http"):
                continue
            if best is None:
                best = thumb
                continue
            curr_area = (best.get("width") or 0) * (best.get("height") or 0)
            new_area = (thumb.get("width") or 0) * (thumb.get("height") or 0)
            if new_area > curr_area:
                best = thumb
        return InstagramParser._clean_url(best.get("url")) if best else None

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
        is_video_url = any(key in final_url for key in ("/reel/", "/reels/", "/tv/"))
        shortcode = self._extract_shortcode(final_url) or self._extract_shortcode(url)
        base_prefix = f"ig_{shortcode}" if shortcode else "ig"
        try:
            info = await self._extract_info(final_url)
        except ParseException:
            og = await self._fetch_og_meta(final_url)
            if og_image := og.get("image"):
                image_name = (
                    f"{base_prefix}{self._url_suffix(og_image, '.jpg')}"
                    if shortcode
                    else None
                )
                image_task = self.downloader.download_img(
                    og_image,
                    img_name=image_name,
                    ext_headers=self.headers,
                    proxy=self.proxy,
                )
                return self.result(
                    title=og.get("title") or None,
                    contents=[ImageContent(image_task)],
                    url=final_url,
                )
            raise
        entries = self._iter_entries(info)
        single_entry = len(entries) == 1

        contents = []
        meta_entry: dict[str, Any] | None = None
        fallback_video_tried = False
        for idx, entry in enumerate(entries):
            entry_id = self._entry_identity(entry, str(idx))
            entry_shortcode = entry.get("shortcode")
            base_name = f"{base_prefix}_{entry_id}"
            video_fmt, audio_fmt = self._pick_formats(entry)
            video_url = self._format_url(video_fmt) if video_fmt else None
            audio_url = self._format_url(audio_fmt) if audio_fmt else None
            image_url = None if video_url else self._pick_image_url(entry)
            thumbnail = entry.get("thumbnail")
            duration = float(entry.get("duration") or 0)
            if not video_url and not image_url and not (is_video_url and thumbnail):
                continue
            if video_url:
                cover_task = None
                if thumbnail:
                    cover_name = f"{base_name}_cover{self._url_suffix(thumbnail, '.jpg')}"
                    cover_task = self.downloader.download_img(
                        thumbnail,
                        img_name=cover_name,
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                if audio_url:
                    output_path = self.downloader.cache_dir / f"{base_name}_av.mp4"
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
                elif is_video_url and single_entry and not fallback_video_tried:
                    fallback_video_tried = True
                    try:
                        video_task = await self._download_with_ytdlp(
                            final_url, f"{base_name}_ydlp.mp4"
                        )
                        contents.append(VideoContent(video_task, cover_task, duration))
                        if meta_entry is None:
                            meta_entry = entry
                        continue
                    except ParseException:
                        pass
                else:
                    video_task = self.downloader.download_video(
                        video_url,
                        video_name=f"{base_name}_v.mp4",
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                    contents.append(VideoContent(video_task, cover_task, duration))
            elif image_url or (is_video_url and thumbnail):
                cover_task = None
                if thumbnail:
                    cover_name = f"{base_name}_cover{self._url_suffix(thumbnail, '.jpg')}"
                    cover_task = self.downloader.download_img(
                        thumbnail,
                        img_name=cover_name,
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                if is_video_url and not fallback_video_tried:
                    fallback_video_tried = True
                    try:
                        video_task = await self._download_with_ytdlp(
                            final_url, f"{base_name}_ydlp.mp4"
                        )
                        contents.append(VideoContent(video_task, cover_task, duration))
                        if meta_entry is None:
                            meta_entry = entry
                        continue
                    except ParseException:
                        pass
                if image_url:
                    image_url = await self._upgrade_image_url(
                        image_url, entry_shortcode or shortcode
                    )
                    image_name = f"{base_name}{self._url_suffix(image_url, '.jpg')}"
                    image_task = self.downloader.download_img(
                        image_url,
                        img_name=image_name,
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                    contents.append(ImageContent(image_task))
                elif thumbnail:
                    contents.append(ImageContent(cover_task))
            if meta_entry is None:
                meta_entry = entry

        meta = meta_entry or info
        if not contents:
            fallback_url = meta.get("webpage_url") or final_url
            if is_video_url and not fallback_video_tried:
                try:
                    thumbnail = meta.get("thumbnail")
                    duration = float(meta.get("duration") or 0)
                    cover_task = None
                    if thumbnail:
                        cover_name = f"{base_prefix}_cover{self._url_suffix(thumbnail, '.jpg')}"
                        cover_task = self.downloader.download_img(
                            thumbnail,
                            img_name=cover_name,
                            ext_headers=self.headers,
                            proxy=self.proxy,
                        )
                    if isinstance(fallback_url, str) and fallback_url:
                        video_task = await self._download_with_ytdlp(
                            fallback_url, f"{base_prefix}_ydlp.mp4"
                        )
                        contents.append(VideoContent(video_task, cover_task, duration))
                except ParseException:
                    pass
            if not contents:
                image_url = self._pick_image_url(meta)
                if isinstance(image_url, str) and image_url:
                    image_url = await self._upgrade_image_url(image_url, shortcode)
                    image_name = f"{base_prefix}{self._url_suffix(image_url, '.jpg')}"
                    image_task = self.downloader.download_img(
                        image_url,
                        img_name=image_name,
                        ext_headers=self.headers,
                        proxy=self.proxy,
                    )
                    contents.append(ImageContent(image_task))
            if not contents:
                raise ParseException("未找到可下载的视频")
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
