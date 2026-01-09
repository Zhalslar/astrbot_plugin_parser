import asyncio
from asyncio import Task, create_task
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import LimitedSizeDict, generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class Downloader:
    """下载器，支持youtube-dlp 和 流式下载"""

    def __init__(self, config: AstrBotConfig):
        self.config = config
        self.cache_dir = Path(config["cache_dir"])
        self.proxy: str | None = self.config["proxy"] or None
        self.max_duration: int = config["source_max_minute"] * 60
        self.max_size = self.config["source_max_size"]
        self.headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()
        # 用于流式下载的客户端
        self.client = ClientSession(
            timeout=ClientTimeout(total=config["download_timeout"])
        )

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download file by url with stream

        Args:
            url (str): url address
            file_name (str | None): file name. Defaults to generate_file_name.
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: file path

        Raises:
            httpx.HTTPError: When download fails
        """

        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cache_dir / file_name
        # 如果文件存在，则直接返回
        if file_path.exists():
            return file_path

        headers = {**self.headers, **(ext_headers or {})}

        # Use sentinel value to detect if proxy was explicitly passed
        if proxy is ...:
            proxy = self.proxy

        try:
            async with self.client.get(
                url, headers=headers, allow_redirects=True, proxy=proxy
            ) as response:
                if response.status >= 400:
                    raise ClientError(f"HTTP {response.status} {response.reason}")
                content_length = response.headers.get("Content-Length")
                content_length = int(content_length) if content_length else 0

                if content_length == 0:
                    logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                    raise ZeroSizeException
                if (file_size := content_length / 1024 / 1024) > self.max_size:
                    logger.warning(
                        f"媒体 url: {url} 大小 {file_size:.2f} MB 超过 {self.max_size} MB, 取消下载"
                    )
                    raise SizeLimitException

                with self.get_progress_bar(file_name, content_length) as bar:
                    async with aiofiles.open(file_path, "wb") as file:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await file.write(chunk)
                            bar.update(len(chunk))

        except ClientError:
            await safe_unlink(file_path)
            logger.exception(f"下载失败 | url: {url}, file_path: {file_path}")
            raise DownloadException("媒体下载失败")
        return file_path

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar

        Args:
            desc (str): 描述
            total (int | None): 总大小. Defaults to None.

        Returns:
            tqdm: 进度条
        """
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        use_ytdlp: bool = False,
        cookiefile: Path | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download video file by url with stream

        Args:
            url (str): url address
            video_name (str | None): video name. Defaults to get name by parse url.
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            use_ytdlp (bool): use ytdlp to download video. Defaults to False.
            cookiefile (Path | None): cookie file path. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: video file path

        Raises:
            httpx.HTTPError: When download fails
        """
        if use_ytdlp:
            return await self._ytdlp_download_video(url, cookiefile)

        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url, file_name=video_name, ext_headers=ext_headers, proxy=proxy
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        use_ytdlp: bool = False,
        cookiefile: Path | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download audio file by url with stream

        Args:
            url (str): url address
            audio_name (str | None ): audio name. Defaults to generate from url.
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: audio file path

        Raises:
            httpx.HTTPError: When download fails
        """
        if use_ytdlp:
            return await self._ytdlp_download_audio(url, cookiefile)

        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url, file_name=audio_name, ext_headers=ext_headers, proxy=proxy
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download file by url with stream

        Args:
            url (str): url address
            file_name (str | None): file name. Defaults to None.
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: file path
        """
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url, file_name=file_name, ext_headers=ext_headers, proxy=proxy
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download image file by url with stream

        Args:
            url (str): url
            img_name (str | None): image name. Defaults to generate from url.
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: image file path

        Raises:
            httpx.HTTPError: When download fails
        """
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(
            url, file_name=img_name, ext_headers=ext_headers, proxy=proxy
        )

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        ext_headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> list[Path]:
        """download images without raise

        Args:
            urls (list[str]): urls
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            list[Path]: image file paths
        """
        paths_or_errs = await asyncio.gather(
            *[
                self.download_img(url, ext_headers=ext_headers, proxy=proxy)
                for url in urls
            ],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        ext_headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
    ) -> Path:
        """download video and audio file by url with stream and merge

        Args:
            v_url (str): video url
            a_url (str): audio url
            output_path (Path): output file path
            ext_headers (dict[str, str] | None): ext headers. Defaults to None.
            proxy (str | None): proxy URL. Defaults to configured proxy. Use None to disable proxy.

        Returns:
            Path: merged file path
        """
        v_path, a_path = await asyncio.gather(
            self.download_video(v_url, ext_headers=ext_headers, proxy=proxy),
            self.download_audio(a_url, ext_headers=ext_headers, proxy=proxy),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

    # region -------------------- 私有：yt-dlp --------------------

    async def ytdlp_extract_info(
        self, url: str, cookiefile: Path | None = None
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "force_generic_extractor": True,
            "cookiefile": None,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def _ytdlp_download_video(
        self, url: str, cookiefile: Path | None = None
    ) -> Path:
        info = await self.ytdlp_extract_info(url, cookiefile)
        if info.duration > self.max_duration:
            raise DurationLimitException

        video_path = self.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            # "format": f"bv[filesize<={info.duration // 10 + 10}M]+ba/b[filesize<={info.duration // 8 + 10}M]",
            "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "cookiefile": None,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        with yt_dlp.YoutubeDL(opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])
        return video_path

    async def _ytdlp_download_audio(self, url: str, cookiefile: Path | None) -> Path:
        file_name = generate_file_name(url)
        audio_path = self.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        opts = {
            "outtmpl": str(self.cache_dir / file_name) + ".%(ext)s",
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ],
            "cookiefile": None,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        with yt_dlp.YoutubeDL(opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])
        return audio_path

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()
