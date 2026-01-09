import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, ClassVar

import yt_dlp
from yt_dlp.utils import DownloadError
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import DownloadException
from ..utils import encode_video_to_h264, generate_file_name, save_cookies_with_netscape
from .base import BaseParser, handle


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="instagram", display_name="Instagram")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.ig_cookies_file: Path | None = None
        self._set_cookies()

    def _set_cookies(self) -> None:
        raw_cookies = self.config.get("ig_ck") or ""
        raw_cookies = raw_cookies.strip()
        if not raw_cookies:
            return

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

    @handle(
        "instagram.com",
        r"https?://(?:www\.)?instagram\.com/(?P<kind>p|reel|tv)/(?P<shortcode>[^/?#]+)/?(?:[?#].*)?",
    )
    @handle(
        "instagr.am",
        r"https?://(?:www\.)?instagr\.am/(?P<kind>p|reel|tv)/(?P<shortcode>[^/?#]+)/?(?:[?#].*)?",
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        kind = searched.group("kind")
        shortcode = searched.group("shortcode")
        page_url = f"https://www.instagram.com/{kind}/{shortcode}/"
        result = await self._parse_from_ytdlp(page_url)
        if result is None:
            logger.warning(
                "Instagram yt-dlp info unavailable, fallback to download-only: %s",
                page_url,
            )
            return self._fallback_ytdlp_result(page_url)
        return result

    async def _parse_from_ytdlp(self, page_url: str) -> ParseResult | None:
        info = await self._fetch_ytdlp_info(page_url)
        if not info:
            return None

        ext_headers = {**self.headers, "Referer": "https://www.instagram.com/"}
        contents = self._extract_ytdlp_contents(info, ext_headers, page_url)
        if not contents:
            return None

        author_name = self._first_text(
            info.get("uploader"),
            info.get("uploader_id"),
            info.get("channel"),
        )
        text = self._first_text(info.get("description"), info.get("title"))
        timestamp = info.get("timestamp")
        timestamp = int(timestamp) if isinstance(timestamp, (int, float)) else None

        author = self.create_author(author_name) if author_name else None
        return self.result(
            url=page_url,
            author=author,
            text=text,
            timestamp=timestamp,
            contents=contents,
        )

    async def _fetch_ytdlp_info(self, url: str) -> dict[str, Any] | None:
        opts = {
            "quiet": True,
            "skip_download": True,
        }
        if self.ig_cookies_file and self.ig_cookies_file.is_file():
            opts["cookiefile"] = str(self.ig_cookies_file)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
                if isinstance(raw, dict):
                    return raw
                return None
            except DownloadError as exc:
                logger.warning(
                    "Instagram yt-dlp extract_info failed (%s/%s): %s",
                    attempt,
                    max_attempts,
                    exc,
                )
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

    def _fallback_ytdlp_result(self, page_url: str) -> ParseResult:
        ext_headers = {**self.headers, "Referer": "https://www.instagram.com/"}
        contents = [
            self._create_ytdlp_video_content(page_url, {}, 0.0, ext_headers)
        ]
        return self.result(url=page_url, contents=contents)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value:
                return value
        return None

    def _extract_ytdlp_contents(
        self, info: dict[str, Any], ext_headers: dict[str, str], page_url: str
    ) -> list[Any]:
        contents: list[Any] = []
        entries = info.get("entries")
        if isinstance(entries, list) and entries:
            for entry in entries:
                if isinstance(entry, dict):
                    contents.extend(
                        self._entry_to_contents(entry, ext_headers, page_url)
                    )
            return contents
        return self._entry_to_contents(info, ext_headers, page_url)

    def _entry_to_contents(
        self,
        entry: dict[str, Any],
        ext_headers: dict[str, str],
        page_url: str,
    ) -> list[Any]:
        contents: list[Any] = []
        duration = entry.get("duration")
        duration_value = (
            float(duration) if isinstance(duration, (int, float)) else 0.0
        )
        if self._is_video_entry(entry):
            entry_url = self._entry_webpage_url(entry, page_url)
            contents.append(
                self._create_ytdlp_video_content(
                    entry_url, entry, duration_value, ext_headers
                )
            )
            return contents

        image_url = self._extract_image_url(entry)
        if image_url:
            contents.extend(self.create_image_contents([image_url], ext_headers))
        return contents

    def _create_ytdlp_video_content(
        self,
        url: str,
        entry: dict[str, Any],
        duration_value: float,
        ext_headers: dict[str, str],
    ):
        async def download() -> Path:
            info = entry if entry.get("formats") else None
            if info is None:
                info = await self._fetch_ytdlp_info(url)
            if not info:
                raise DownloadException("媒体下载失败")

            v_url, a_url = self._select_media_urls(info)
            if not v_url:
                raise DownloadException("媒体下载失败")

            if a_url:
                output_path = self._merged_output_path(v_url, a_url)
                if output_path.exists():
                    return output_path
                return await self.downloader.download_av_and_merge(
                    v_url,
                    a_url,
                    output_path=output_path,
                    ext_headers=ext_headers,
                    proxy=self.proxy,
                    reencode_h264=True,
                )

            return await self._download_single_video(v_url, ext_headers)

        video_task = asyncio.create_task(download(), name=f"instagram_av | {url}")
        return self.create_video_content(
            video_task,
            cover_url=entry.get("thumbnail"),
            duration=duration_value,
            ext_headers=ext_headers,
        )

    async def _download_single_video(
        self, v_url: str, ext_headers: dict[str, str]
    ) -> Path:
        file_name = generate_file_name(v_url, ".mp4")
        base_path = self.downloader.cache_dir / file_name
        output_path = base_path.with_name(f"{base_path.stem}_h264{base_path.suffix}")
        if output_path.exists():
            return output_path
        video_path = await self.downloader.streamd(
            v_url,
            file_name=file_name,
            ext_headers=ext_headers,
            proxy=self.proxy,
        )
        return await encode_video_to_h264(video_path)

    def _merged_output_path(self, v_url: str, a_url: str) -> Path:
        digest = hashlib.md5(f"{v_url}|{a_url}".encode()).hexdigest()[:16]
        return self.downloader.cache_dir / f"{digest}.mp4"

    def _select_media_urls(
        self, info: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        formats = info.get("formats")
        if isinstance(formats, list) and formats:
            video_fmt = self._best_video_format(formats)
            audio_fmt = self._best_audio_format(formats)
            if video_fmt and audio_fmt:
                return video_fmt["url"], audio_fmt["url"]
            if video_fmt and not audio_fmt:
                logger.warning("Instagram audio format not found, fallback to combined")
            combined_fmt = self._best_av_format(formats)
            if combined_fmt:
                logger.warning("Instagram using combined format for download")
                return combined_fmt["url"], None

        direct_url = self._extract_video_url(info)
        if direct_url:
            logger.warning("Instagram formats missing, using direct URL download")
            return direct_url, None
        return None, None

    @staticmethod
    def _format_url(fmt: dict[str, Any]) -> str | None:
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
            if self._format_url(fmt) is None:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if vcodec in (None, "none"):
                continue
            if acodec not in (None, "none"):
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
                int(tbr) if isinstance(tbr, (int, float)) else 0,
            )

        best = max(candidates, key=sort_key)
        return best

    @staticmethod
    def _best_audio_format(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if acodec in (None, "none"):
                continue
            if vcodec not in (None, "none"):
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
                int(abr) if isinstance(abr, (int, float)) else 0,
                int(tbr) if isinstance(tbr, (int, float)) else 0,
            )

        best = max(candidates, key=sort_key)
        return best

    def _best_av_format(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if self._format_url(fmt) is None:
                continue
            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            if vcodec in (None, "none") or acodec in (None, "none"):
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
                int(tbr) if isinstance(tbr, (int, float)) else 0,
            )

        best = max(candidates, key=sort_key)
        return best

    @staticmethod
    def _entry_webpage_url(entry: dict[str, Any], fallback: str) -> str:
        url = entry.get("webpage_url") or entry.get("original_url")
        if isinstance(url, str) and url:
            return url
        return fallback

    def _is_video_entry(self, entry: dict[str, Any]) -> bool:
        url = entry.get("url")
        if isinstance(url, str) and url:
            ext = entry.get("ext")
            mime_type = entry.get("mime_type")
            vcodec = entry.get("vcodec")
            if vcodec not in ("none", None):
                return True
            if isinstance(ext, str) and ext.lower() in {"mp4", "m4v", "webm"}:
                return True
            if isinstance(mime_type, str) and mime_type.startswith("video/"):
                return True
            if ".mp4" in url or ".m4v" in url or ".webm" in url:
                return True
        formats = entry.get("formats")
        if isinstance(formats, list):
            return self._best_video_format_url(formats) is not None
        return False

    @staticmethod
    def _extract_image_url(entry: dict[str, Any]) -> str | None:
        url = entry.get("url")
        ext = entry.get("ext")
        if isinstance(url, str) and url:
            if isinstance(ext, str) and ext.lower() in {"jpg", "jpeg", "png", "webp"}:
                return url
        thumb = entry.get("thumbnail")
        return thumb if isinstance(thumb, str) and thumb else None

    def _extract_video_url(self, entry: dict[str, Any]) -> str | None:
        url = entry.get("url")
        if isinstance(url, str) and url:
            ext = entry.get("ext")
            mime_type = entry.get("mime_type")
            vcodec = entry.get("vcodec")
            if vcodec not in ("none", None):
                return url
            if isinstance(ext, str) and ext.lower() in {"mp4", "m4v", "webm"}:
                return url
            if isinstance(mime_type, str) and mime_type.startswith("video/"):
                return url
            if ".mp4" in url or ".m4v" in url or ".webm" in url:
                return url
        return self._best_video_format_url(entry.get("formats"))

    @staticmethod
    def _best_video_format_url(formats: Any) -> str | None:
        if not isinstance(formats, list):
            return None
        candidates = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url:
                continue
            vcodec = fmt.get("vcodec")
            if vcodec in (None, "none"):
                ext = fmt.get("ext")
                mime_type = fmt.get("mime_type")
                if isinstance(ext, str) and ext.lower() in {"mp4", "m4v", "webm"}:
                    pass
                elif isinstance(mime_type, str) and mime_type.startswith("video/"):
                    pass
                else:
                    continue
            candidates.append(fmt)
        if not candidates:
            return None

        def sort_key(fmt: dict[str, Any]) -> int:
            height = fmt.get("height")
            return int(height) if isinstance(height, int) else 0

        best = max(candidates, key=sort_key)
        return best.get("url")
