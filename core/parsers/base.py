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
from ..data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    ParseResultKwargs,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..exception import ParseException

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
        self.data_dir = Path(config["data_dir"])
        self.downloader = downloader
        # Proxy only applies to YouTube and TikTok as per configuration
        proxy_enabled_platforms = ["youtube", "tiktok"]
        if self.__class__.platform.name in proxy_enabled_platforms:
            self.proxy = config.get("proxy") or None
        else:
            self.proxy = None
        # 每个实例拥有独立的 session
        self._session: ClientSession | None = None
        self._timeout = config["common_timeout"]

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

    @property
    def client(self) -> ClientSession:
        """获取当前实例的 session，惰性创建"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=ClientTimeout(total=self._timeout))
        return self._session

    async def close_session(self) -> None:
        """关闭当前实例的 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

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
        async with self.client.get(
            url, headers=headers, allow_redirects=False, proxy=self.proxy
        ) as resp:
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
        async with self.client.get(
            url, headers=headers, allow_redirects=True, proxy=self.proxy
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"final url check {resp.status} {resp.reason}")
            return str(resp.url)

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ):
        """创建作者对象"""

        avatar_task = None
        if avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url, ext_headers=ext_headers or self.headers, proxy=self.proxy
            )
        return Author(name=name, avatar=avatar_task, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        ext_headers: dict[str, str] | None = None,
    ):
        """创建视频内容"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, ext_headers=ext_headers or self.headers, proxy=self.proxy
            )
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task, ext_headers=ext_headers or self.headers, proxy=self.proxy
            )

        return VideoContent(url_or_task, cover_task, duration)

    def create_image_contents(
        self,
        image_urls: list[str],
        ext_headers: dict[str, str] | None = None,
    ):
        """创建图片内容列表"""
        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(url, ext_headers=ext_headers or self.headers, proxy=self.proxy)
            contents.append(ImageContent(task))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        ext_headers: dict[str, str] | None = None,
    ):
        """创建动态图片内容列表"""
        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(url, ext_headers=ext_headers or self.headers, proxy=self.proxy)
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
    ):
        """创建音频内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task, ext_headers=self.headers, proxy=self.proxy
            )

        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        image_task = self.downloader.download_img(image_url, ext_headers=self.headers, proxy=self.proxy)
        return GraphicsContent(image_task, text, alt)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
    ):
        """创建文件内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task, ext_headers=self.headers, file_name=name, proxy=self.proxy
            )

        return FileContent(url_or_task)
