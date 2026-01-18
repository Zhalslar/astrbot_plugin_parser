import asyncio
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import yt_dlp

from astrbot.api import logger

from ..config import PluginConfig
from ..data import ImageContent, Platform, VideoContent
from ..download import Downloader
from ..exception import DownloadException, ParseException
from ..utils import generate_file_name, safe_unlink, save_cookies_with_netscape
from .base import BaseParser, handle


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="instagram", display_name="Instagram")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.instagram
        self.headers.update(
            {
                "Origin": "https://www.instagram.com",
                "Referer": "https://www.instagram.com/",
            }
        )
        self.ig_cookies_file: Path | None = None
        self.ig_cookie_header: str | None = None
        self._set_cookies()

    def _set_cookies(self) -> None:
        raw_cookies = self.mycfg.cookies
        if not raw_cookies:
            return

        cookie_path = Path(raw_cookies)
        if cookie_path.is_file():
            self.ig_cookies_file = cookie_path
            return

        self.ig_cookie_header = self._cookie_header_from_raw(raw_cookies)
        cookie_file = self.data_dir / "ig_cookies.txt"
        cookie_file.parent.mkdir(parents=True, exist_ok=True)

        normalized = self._normalize_netscape(raw_cookies)
        if normalized:
            cookie_file.write_text(normalized)
        else:
            cookies = raw_cookies.replace("\n", "").replace("\r", "").strip()
            if not cookies:
                return
            save_cookies_with_netscape(cookies, cookie_file, "instagram.com")

        self.ig_cookies_file = cookie_file

    @staticmethod
    def _normalize_netscape(raw_cookies: str) -> str | None:
        lines: list[str] = []
        for line in raw_cookies.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            domain, include_subdomains, path, secure, expires, name = parts[:6]
            value = " ".join(parts[6:])
            lines.append(
                "\t".join(
                    [
                        domain,
                        include_subdomains,
                        path,
                        secure,
                        expires,
                        name,
                        value,
                    ]
                )
            )
        if not lines:
            return None
        header = (
            "# Netscape HTTP Cookie File\n"
            "# https://curl.haxx.se/rfc/cookie_spec.html\n"
            "# This is a generated file! Do not edit.\n\n"
        )
        return header + "\n".join(lines) + "\n"

    @staticmethod
    def _cookie_header_from_raw(raw_cookies: str) -> str:
        raw_cookies = raw_cookies.strip()
        if not raw_cookies:
            return ""
        if raw_cookies.startswith("#") or "\t" in raw_cookies or "\n" in raw_cookies:
            pairs: list[str] = []
            for line in raw_cookies.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                name = parts[5]
                value = " ".join(parts[6:])
                pairs.append(f"{name}={value}")
            return "; ".join(pairs)
        return raw_cookies.replace("\n", "").replace("\r", "").strip()

    async def _gallery_dl_image_urls(self, url: str) -> list[str]:
        cmd = [sys.executable, "-m", "gallery_dl", "-j"]
        if self.ig_cookies_file and self.ig_cookies_file.is_file():
            cmd += ["--cookies", str(self.ig_cookies_file)]
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise ParseException(
                f"gallery-dl 解析失败: {stderr.decode(errors='ignore').strip()}"
            )

        text = stdout.decode(errors="ignore").strip()
        if not text:
            raise ParseException("gallery-dl 输出为空")

        urls: list[str] = []
        errors: list[str] = []

        def handle_item(item: object) -> None:
            if isinstance(item, list):
                if len(item) >= 2 and item[0] == -1 and isinstance(item[1], dict):
                    message = item[1].get("message")
                    if isinstance(message, str):
                        errors.append(message)
                    return
                if (
                    len(item) >= 3
                    and item[0] == 3
                    and isinstance(item[1], str)
                ):
                    urls.append(self._clean_url(item[1]))
                    return
                if len(item) >= 2 and item[0] == 3 and isinstance(item[1], dict):
                    for key in ("url", "display_url"):
                        val = item[1].get(key)
                        if isinstance(val, str):
                            urls.append(self._clean_url(val))
                            return
            if isinstance(item, dict):
                for key in ("url", "display_url"):
                    val = item.get(key)
                    if isinstance(val, str):
                        urls.append(self._clean_url(val))
                        return

        try:
            data = json.loads(text)
            if isinstance(data, list):
                for item in data:
                    handle_item(item)
            else:
                handle_item(data)
        except json.JSONDecodeError:
            for line in text.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handle_item(item)

        if not urls:
            if errors:
                raise ParseException(f"gallery-dl 解析失败: {errors[0]}")
            raise ParseException("gallery-dl 未返回图片链接")
        return urls

    async def _fetch_ytdlp_info(
        self, url: str, max_attempts: int = 3
    ) -> dict[str, Any] | None:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": {**self.headers, "Referer": "https://www.instagram.com/"},
        }
        if self.ig_cookie_header:
            opts["http_headers"]["Cookie"] = self.ig_cookie_header
        if self.ig_cookies_file and self.ig_cookies_file.is_file():
            opts["cookiefile"] = str(self.ig_cookies_file)
        for attempt in range(1, max_attempts + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl: # type: ignore
                    raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
                if isinstance(raw, dict):
                    return raw # type: ignore
                return None
            except Exception as exc:
                logger.warning(
                    "Instagram yt-dlp extract_info error (%s/%s): %s",
                    attempt,
                    max_attempts,
                    exc,
                )
            if attempt < max_attempts:
                await asyncio.sleep(min(2 * attempt, 5))
        return None



    async def _download_with_ytdlp(
        self, url: str, output_name: str | None = None
    ) -> Path:
        if output_name:
            output_path = self.cfg.cache_dir / output_name
        else:
            output_path = self.cfg.cache_dir / generate_file_name(url, ".mp4")
        if output_path.exists():
            return output_path
        retries = 2
        opts: dict[str, Any] = {
            "quiet": True,
            "outtmpl": str(output_path),
            "merge_output_format": "mp4",
            "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
            "http_headers": {**self.headers, "Referer": "https://www.instagram.com/"},
        }
        if self.ig_cookie_header:
            opts["http_headers"]["Cookie"] = self.ig_cookie_header
        if self.ig_cookies_file and self.ig_cookies_file.is_file():
            opts["cookiefile"] = str(self.ig_cookies_file)
        for attempt in range(retries + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl: # type: ignore
                    await asyncio.to_thread(ydl.download, [url])
                return output_path
            except Exception as exc:
                await safe_unlink(output_path)
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("媒体下载失败") from exc
        raise DownloadException("媒体下载失败")

    @staticmethod
    def _iter_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return [e for e in entries if isinstance(e, dict)]
        return [info]

    @staticmethod
    def _clean_url(url: str) -> str:
        return html.unescape(url)

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
    def _entry_video_url(entry: dict[str, Any]) -> str | None:
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            return None
        ext = entry.get("ext")
        mime_type = entry.get("mime_type")
        vcodec = entry.get("vcodec")
        if vcodec not in (None, "none"):
            return url
        if isinstance(ext, str) and ext.lower() in {"mp4", "m4v", "webm"}:
            return url
        if isinstance(mime_type, str) and mime_type.startswith("video/"):
            return url
        if ".mp4" in url or ".m4v" in url or ".webm" in url:
            return url
        return None

    @staticmethod
    def _codec_is_none(codec: Any) -> bool:
        return codec in (None, "none", "audio only", "video only")

    @staticmethod
    def _format_url_with_protocol(fmt: dict[str, Any]) -> str | None:
        url = fmt.get("url")
        if not isinstance(url, str) or not url:
            return None
        protocol = fmt.get("protocol")
        if isinstance(protocol, str) and not protocol.startswith("http"):
            return None
        return url

    def _best_video_format(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if self._format_url_with_protocol(fmt) is None:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if self._codec_is_none(vcodec):
                continue
            if not self._codec_is_none(acodec):
                continue
            candidates.append(fmt)
        if not candidates:
            return None

        def sort_key(fmt: dict[str, Any]) -> tuple[int, int, int]:
            vcodec = fmt.get("vcodec") or ""
            prefer_avc = 1 if isinstance(vcodec, str) and ("avc" in vcodec or "h264" in vcodec) else 0
            height = fmt.get("height")
            tbr = fmt.get("tbr")
            return (
                prefer_avc,
                int(height) if isinstance(height, int) else 0,
                int(tbr) if isinstance(tbr, int | float) else 0,
            )

        return max(candidates, key=sort_key)

    @classmethod
    def _best_audio_format(
        cls, formats: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if cls._codec_is_none(acodec):
                continue
            if not cls._codec_is_none(vcodec):
                continue
            protocol = fmt.get("protocol")
            if isinstance(protocol, str) and not protocol.startswith("http"):
                continue
            candidates.append(fmt)
        if not candidates:
            return None

        def sort_key(fmt: dict[str, Any]) -> tuple[int, int]:
            abr = fmt.get("abr")
            tbr = fmt.get("tbr")
            return (
                int(abr) if isinstance(abr, int | float) else 0,
                int(tbr) if isinstance(tbr, int | float) else 0,
            )

        return max(candidates, key=sort_key)

    def _best_av_format(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if self._format_url_with_protocol(fmt) is None:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if self._codec_is_none(vcodec) or self._codec_is_none(acodec):
                continue
            candidates.append(fmt)
        if not candidates:
            return None

        def sort_key(fmt: dict[str, Any]) -> tuple[int, int, int]:
            vcodec = fmt.get("vcodec") or ""
            prefer_avc = 1 if isinstance(vcodec, str) and ("avc" in vcodec or "h264" in vcodec) else 0
            height = fmt.get("height")
            tbr = fmt.get("tbr")
            return (
                prefer_avc,
                int(height) if isinstance(height, int) else 0,
                int(tbr) if isinstance(tbr, int | float) else 0,
            )

        return max(candidates, key=sort_key)

    def _select_media_urls(
        self, info: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        formats = info.get("formats")
        if isinstance(formats, list) and formats:
            video_fmt = self._best_video_format(formats)
            audio_fmt = self._best_audio_format(formats)
            if video_fmt and audio_fmt:
                logger.info(
                    "Instagram selected formats v=%s a=%s",
                    video_fmt.get("format_id"),
                    audio_fmt.get("format_id"),
                )
                return video_fmt["url"], audio_fmt["url"]
            if video_fmt and not audio_fmt:
                logger.warning("Instagram audio format not found, fallback to combined")
            combined_fmt = self._best_av_format(formats)
            if combined_fmt:
                logger.warning("Instagram using combined format for download")
                return combined_fmt["url"], None

        direct_url = self._entry_video_url(info)
        if direct_url:
            logger.warning("Instagram formats missing, using direct URL download")
            return direct_url, None
        return None, None

    def _merged_output_path(self, v_url: str, a_url: str) -> Path:
        digest = hashlib.md5(f"{v_url}|{a_url}".encode()).hexdigest()[:16]
        return self.cfg.cache_dir / f"{digest}.mp4"


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
        if matched := re.search(r"/(p|reel|reels|tv)/", final_url):
            kind = matched.group(1)
        else:
            kind = ""
        is_video_url = kind in {"reel", "reels", "tv"}
        shortcode = self._extract_shortcode(final_url) or self._extract_shortcode(url)
        base_prefix = f"ig_{shortcode}" if shortcode else "ig"
        info = await self._fetch_ytdlp_info(final_url)
        contents = []
        if info is None:
            if not is_video_url:
                gallery_urls = await self._gallery_dl_image_urls(final_url)
                for idx, image_url in enumerate(gallery_urls, start=1):
                    image_name = (
                        f"{base_prefix}_{idx}{Path(urlparse(image_url).path).suffix}"
                        if shortcode
                        else None
                    )
                    image_task = self.downloader.download_img(
                        image_url,
                        img_name=image_name,
                        headers=self.headers,
                        proxy=self.proxy,
                    )
                    contents.append(ImageContent(image_task))
                return self.result(contents=contents, url=final_url)
            try:
                video_task = await self._download_with_ytdlp(
                    final_url, f"{base_prefix}_ydlp.mp4"
                )
                contents.append(VideoContent(video_task, None, 0))
                return self.result(contents=contents, url=final_url)
            except ParseException as exc:
                raise ParseException("未找到可下载的视频") from exc
        entries = self._iter_entries(info)
        single_entry = len(entries) == 1

        meta_entry: dict[str, Any] | None = None
        fallback_video_tried = False
        for idx, entry in enumerate(entries):
            entry_id = self._entry_identity(entry, str(idx))
            base_name = f"{base_prefix}_{entry_id}"
            formats = entry.get("formats")
            video_url, audio_url = self._select_media_urls(entry)
            if not video_url and isinstance(formats, list) and formats:
                video_fmt = self._best_av_format(formats)
                if video_fmt:
                    video_url = video_fmt.get("url")
            duration = float(entry.get("duration") or 0)
            if not video_url:
                continue
            if video_url:
                cover_task = None
                if audio_url:
                    output_path = self._merged_output_path(video_url, audio_url)
                    if output_path.exists():
                        video_task = output_path
                    else:
                        video_task = self.downloader.download_av_and_merge(
                            video_url,
                            audio_url,
                            output_path=output_path,
                            headers=self.headers,
                            proxy=self.proxy,
                        )
                    contents.append(VideoContent(video_task, cover_task, duration))
                else:
                    v_url, a_url = (None, None)
                    if single_entry:
                        v_url, a_url = self._select_media_urls(info)
                    if a_url and v_url:
                        output_path = self._merged_output_path(v_url, a_url)
                        if output_path.exists():
                            video_task = output_path
                        else:
                            video_task = self.downloader.download_av_and_merge(
                                v_url,
                                a_url,
                                output_path=output_path,
                                headers=self.headers,
                                proxy=self.proxy,
                            )
                        contents.append(VideoContent(video_task, cover_task, duration))
                        if meta_entry is None:
                            meta_entry = entry
                        continue

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
            if meta_entry is None:
                meta_entry = entry

        meta = meta_entry or info
        if not contents:
            fallback_url = meta.get("webpage_url") or final_url
            if is_video_url and not fallback_video_tried:
                try:
                    duration = float(meta.get("duration") or 0)
                    if isinstance(fallback_url, str) and fallback_url:
                        video_task = await self._download_with_ytdlp(
                            fallback_url, f"{base_prefix}_ydlp.mp4"
                        )
                        contents.append(VideoContent(video_task, None, duration))
                except ParseException:
                    pass
            if not contents and not is_video_url:
                gallery_urls = await self._gallery_dl_image_urls(final_url)
                for idx, image_url in enumerate(gallery_urls, start=1):
                    image_name = (
                        f"{base_prefix}_{idx}{Path(urlparse(image_url).path).suffix}"
                        if shortcode
                        else None
                    )
                    image_task = self.downloader.download_img(
                        image_url,
                        img_name=image_name,
                        headers=self.headers,
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
