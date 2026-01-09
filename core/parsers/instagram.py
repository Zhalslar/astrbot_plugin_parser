import asyncio
import re
from pathlib import Path
from typing import Any, ClassVar

import yt_dlp
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from ..utils import save_cookies_with_netscape
from .base import BaseParser, handle


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="instagram", display_name="Instagram")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.ig_cookies_file: Path | None = None
        if self.config.get("ig_cookies_file"):
            self.ig_cookies_file = Path(self.config["ig_cookies_file"])
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

        self.config["ig_cookies_file"] = str(cookie_file)
        self.config.save_config()
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
            raise ParseException("instagram content not accessible")
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
            "force_generic_extractor": True,
        }
        if self.ig_cookies_file and self.ig_cookies_file.is_file():
            opts["cookiefile"] = str(self.ig_cookies_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

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
            prefer_ytdlp = len(entries) == 1
            for entry in entries:
                if isinstance(entry, dict):
                    contents.extend(
                        self._entry_to_contents(
                            entry, ext_headers, page_url, prefer_ytdlp
                        )
                    )
            return contents
        return self._entry_to_contents(info, ext_headers, page_url, True)

    def _entry_to_contents(
        self,
        entry: dict[str, Any],
        ext_headers: dict[str, str],
        page_url: str,
        prefer_ytdlp: bool,
    ) -> list[Any]:
        contents: list[Any] = []
        duration = entry.get("duration")
        duration_value = (
            float(duration) if isinstance(duration, (int, float)) else 0.0
        )
        if self._is_video_entry(entry):
            if prefer_ytdlp:
                entry_url = self._entry_webpage_url(entry, page_url)
                contents.append(
                    self._create_ytdlp_video_content(
                        entry_url, entry, duration_value, ext_headers
                    )
                )
                return contents

            video_url = self._extract_video_url(entry)
            if video_url:
                contents.append(
                    self.create_video_content(
                        video_url,
                        cover_url=entry.get("thumbnail"),
                        duration=duration_value,
                        ext_headers=ext_headers,
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
        video_task = self.downloader.download_video(
            url,
            use_ytdlp=True,
            ytdlp_format=(
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720][ext=mp4]/best[ext=mp4]"
            ),
            cookiefile=self.ig_cookies_file,
            proxy=self.proxy,
        )
        return self.create_video_content(
            video_task,
            cover_url=entry.get("thumbnail"),
            duration=duration_value,
            ext_headers=ext_headers,
        )

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
        prefer = [
            fmt
            for fmt in candidates
            if isinstance(fmt.get("height"), int) and fmt["height"] <= 720
        ]
        target = prefer if prefer else candidates

        def sort_key(fmt: dict[str, Any]) -> int:
            height = fmt.get("height")
            return int(height) if isinstance(height, int) else 0

        best = max(target, key=sort_key)
        return best.get("url")
