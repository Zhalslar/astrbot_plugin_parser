"""Parser 基类定义"""

from abc import ABC
from asyncio import Task
from collections.abc import Callable, Coroutine
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from aiohttp import ClientError, ClientSession, ClientTimeout
from typing_extensions import Unpack

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..constants import PlatformEnum as PlatformEnum
from ..download import Downloader
from ..exception import DownloadException as DownloadException
from ..exception import DurationLimitException as DurationLimitException
from ..exception import ParseException as ParseException
from ..exception import SizeLimitException as SizeLimitException
from ..exception import TipException as TipException
from ..exception import ZeroSizeException as ZeroSizeException
from .data import ParseResult, ParseResultKwargs, Platform

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, ParseResult]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"


# 注册处理器装饰器
def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""

    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])

        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))

        return func

    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类

    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    """

    _registry: ClassVar[list[type["BaseParser"]]] = []
    """ 存储所有已注册的 Parser 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """

    _session: ClassVar[ClientSession | None] = None
    """ 全局 ClientSession 对象 """

    if TYPE_CHECKING:
        _key_patterns: ClassVar[KeyPatterns]
        _handlers: ClassVar[dict[str, HandlerFunc]]

    def __init__(
        self,
        config: AstrBotConfig,
        downloader: Downloader,
    ):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.config = config
        self.downloader = downloader
        self.client = self.get_session(config["common_timeout"])

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        # 获取所有被 handle 装饰的方法
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                handler = cast(HandlerFunc, attr)
                for keyword, pattern in key_patterns:
                    cls._handlers[keyword] = handler
                    cls._key_patterns.append((keyword, pattern))

        # 按关键字长度降序排序
        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    @classmethod
    def get_session(cls, timeout: float = 30) -> ClientSession:
        """取全局单例，首次调用时创建"""
        if cls._session is None or cls._session.closed:
            cls._session = ClientSession(timeout=ClientTimeout(total=timeout))
        return cls._session

    @classmethod
    async def close_session(cls) -> None:
        """关闭全局单例，插件卸载时调用一次即可"""
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        """解析 URL 提取信息

        Args:
            keyword: 关键词
            searched: 正则表达式匹配对象，由平台对应的模式匹配得到

        Returns:
            ParseResult: 解析结果

        Raises:
            ParseException: 解析失败时抛出
        """
        return await self._handlers[keyword](self, searched)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        """先重定向再解析"""
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        """搜索 URL 匹配模式"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        """构建解析结果"""
        return ParseResult(platform=cls.platform, **kwargs)

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 单次重定向"""

        headers = headers or COMMON_HEADER.copy()
        async with self.client.get(url, headers=headers, allow_redirects=False) as resp:
            if resp.status >= 400:
                raise ClientError(f"redirect check {resp.status} {resp.reason}")
            return resp.headers.get("Location", url)

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 允许多次重定向"""
        headers = headers or COMMON_HEADER.copy()
        async with self.client.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise ClientError(f"final url check {resp.status} {resp.reason}")
            return str(resp.url)

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
    ):
        """创建作者对象"""
        from .data import Author

        avatar_task = None
        if avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url, ext_headers=self.headers
            )
        return Author(name=name, avatar=avatar_task, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
    ):
        """创建视频内容"""
        from .data import VideoContent

        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, ext_headers=self.headers
            )
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task, ext_headers=self.headers
            )

        return VideoContent(url_or_task, cover_task, duration)

    def create_image_contents(
        self,
        image_urls: list[str],
    ):
        """创建图片内容列表"""
        from .data import ImageContent

        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(url, ext_headers=self.headers)
            contents.append(ImageContent(task))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
    ):
        """创建动态图片内容列表"""
        from .data import DynamicContent

        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(url, ext_headers=self.headers)
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
    ):
        """创建音频内容"""
        from .data import AudioContent

        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task, ext_headers=self.headers
            )

        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        from .data import GraphicsContent

        image_task = self.downloader.download_img(image_url, ext_headers=self.headers)
        return GraphicsContent(image_task, text, alt)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name : str | None = None,
    ):
        """创建文件内容"""
        from .data import FileContent

        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task, ext_headers=self.headers, file_name=name
            )

        return FileContent(url_or_task)
