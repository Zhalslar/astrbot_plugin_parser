import hashlib
from asyncio import Task
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict


def repr_path_task(path_task: Path | Task[Path]) -> str:
    if isinstance(path_task, Path):
        return f"path={path_task.name}"
    else:
        return f"task={path_task.get_name()}, done={path_task.done()}"


@dataclass(repr=False, slots=True)
class MediaContent:
    path_task: Path | Task[Path]

    async def get_path(self) -> Path:
        if isinstance(self.path_task, Path):
            return self.path_task
        self.path_task = await self.path_task
        return self.path_task

    def __repr__(self) -> str:
        prefix = self.__class__.__name__
        return f"{prefix}({repr_path_task(self.path_task)})"


@dataclass(repr=False, slots=True)
class AudioContent(MediaContent):
    """音频内容"""

    duration: float = 0.0


@dataclass(repr=False, slots=True)
class FileContent(MediaContent):
    """文件内容"""

    name: str | None = None
    """文件名"""


@dataclass(repr=False, slots=True)
class VideoContent(MediaContent):
    """视频内容"""

    cover: Path | Task[Path] | None = None
    """视频封面"""
    duration: float = 0.0
    """时长 单位: 秒"""

    async def get_cover_path(self) -> Path | None:
        if self.cover is None:
            return None
        if isinstance(self.cover, Path):
            return self.cover
        self.cover = await self.cover
        return self.cover

    @property
    def display_duration(self) -> str:
        minutes = int(self.duration) // 60
        seconds = int(self.duration) % 60
        return f"时长: {minutes}:{seconds:02d}"

    def __repr__(self) -> str:
        repr = f"VideoContent(path={repr_path_task(self.path_task)}"
        if self.cover is not None:
            repr += f", cover={repr_path_task(self.cover)}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ImageContent(MediaContent):
    """图片内容"""

    pass


@dataclass(repr=False, slots=True)
class DynamicContent(MediaContent):
    """动态内容 视频格式 后续转 gif"""

    gif_path: Path | None = None


@dataclass(repr=False, slots=True)
class GraphicsContent(MediaContent):
    """图文内容 渲染时文字在前 图片在后"""

    text: str | None = None
    """图片前的文本内容"""
    alt: str | None = None
    """图片描述 渲染时居中显示"""

    def __repr__(self) -> str:
        repr = f"GraphicsContent(path={repr_path_task(self.path_task)}"
        if self.text:
            repr += f", text={self.text}"
        if self.alt:
            repr += f", alt={self.alt}"
        return repr + ")"


@dataclass(slots=True)
class Platform:
    """平台信息"""

    name: str
    """ 平台名称 """
    display_name: str
    """ 平台显示名称 """


@dataclass(repr=False, slots=True)
class Author:
    """作者信息"""

    name: str
    """作者名称"""
    avatar: Path | Task[Path] | None = None
    """作者头像 URL 或本地路径"""
    description: str | None = None
    """作者个性签名等"""

    async def get_avatar_path(self) -> Path | None:
        if self.avatar is None:
            return None
        if isinstance(self.avatar, Path):
            return self.avatar
        self.avatar = await self.avatar
        return self.avatar

    def __repr__(self) -> str:
        repr = f"Author(name={self.name}"
        if self.avatar:
            repr += f", avatar_{repr_path_task(self.avatar)}"
        if self.description:
            repr += f", description={self.description}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ParseResult:
    """完整的解析结果"""

    platform: Platform
    """平台信息"""
    author: Author | None = None
    """作者信息"""
    title: str | None = None
    """标题"""
    text: str | None = None
    """文本内容"""
    timestamp: int | None = None
    """发布时间戳, 秒"""
    url: str | None = None
    """来源链接"""
    contents: list[MediaContent] = field(default_factory=list)
    """媒体内容"""
    extra: dict[str, Any] = field(default_factory=dict)
    """额外信息"""
    repost: "ParseResult | None" = None
    """转发的内容"""
    render_image: Path | None = None
    """渲染图片"""
    _resource_id: str | None = field(init=False, repr=False)
    """资源 ID"""
    @property
    def header(self) -> str | None:
        """头信息 仅用于 default render"""
        header = self.platform.display_name
        if self.author:
            header += f" @{self.author.name}"
        if self.title:
            header += f" | {self.title}"
        return header

    @property
    def display_url(self) -> str | None:
        return f"链接: {self.url}" if self.url else None

    @property
    def repost_display_url(self) -> str | None:
        return f"原帖: {self.repost.url}" if self.repost and self.repost.url else None

    @property
    def extra_info(self) -> str | None:
        return self.extra.get("info")

    @property
    def video_contents(self) -> list[VideoContent]:
        return [cont for cont in self.contents if isinstance(cont, VideoContent)]

    @property
    def img_contents(self) -> list[ImageContent]:
        return [cont for cont in self.contents if isinstance(cont, ImageContent)]

    @property
    def audio_contents(self) -> list[AudioContent]:
        return [cont for cont in self.contents if isinstance(cont, AudioContent)]

    @property
    def file_contents(self) -> list[FileContent]:
        return [cont for cont in self.contents if isinstance(cont, FileContent)]

    @property
    def dynamic_contents(self) -> list[DynamicContent]:
        return [cont for cont in self.contents if isinstance(cont, DynamicContent)]

    @property
    def graphics_contents(self) -> list[GraphicsContent]:
        return [cont for cont in self.contents if isinstance(cont, GraphicsContent)]

    @property
    async def cover_path(self) -> Path | None:
        """获取封面路径"""
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                return await cont.get_cover_path()
        return None


    def formatted_datetime(self, fmt: str = "%Y-%m-%d %H:%M:%S") -> str | None:
        """格式化时间戳"""
        return (
            datetime.fromtimestamp(self.timestamp).strftime(fmt)
            if self.timestamp is not None
            else None
        )

    def __repr__(self) -> str:
        return (
            f"platform: {self.platform.display_name}, "
            f"timestamp: {self.timestamp}, "
            f"title: {self.title}, "
            f"text: {self.text}, "
            f"url: {self.url}, "
            f"author: {self.author}, "
            f"contents: {self.contents}, "
            f"extra: {self.extra}, "
            f"repost: <<<<<<<{self.repost}>>>>>>, "
            f"render_image: {self.render_image.name if self.render_image else 'None'}"
        )

    def __post_init__(self):
        object.__setattr__(self, "_resource_id", None)

    def get_resource_id(self) -> str:
        """
        轻量、稳定、无 IO 的资源指纹
        用于判断是否为同一渲染输入
        """
        if self._resource_id is not None:
            return self._resource_id

        h = hashlib.blake2b(digest_size=8)

        def add(v: object | None):
            if v is not None:
                h.update(str(v).encode("utf-8"))
            h.update(b"|")

        add(self.platform.name)
        add(self.url)
        add(self.timestamp)
        if self.author:
            add(self.author.name)

        # ---------- 内容结构 ----------
        add(len(self.contents))
        for cont in self.contents:
            add(cont.__class__.__name__)

            # 子类补充（仍然是 O(1)）
            if isinstance(cont, VideoContent):
                add(cont.duration)
            elif isinstance(cont, AudioContent):
                add(cont.duration)
            elif isinstance(cont, FileContent):
                add(cont.name)
            elif isinstance(cont, GraphicsContent):
                add(cont.text)
                add(cont.alt)

        # ---------- 转发 ----------
        if self.repost:
            add(self.repost.get_resource_id())

        self._resource_id = h.hexdigest()
        return self._resource_id



class ParseResultKwargs(TypedDict, total=False):
    title: str | None
    text: str | None
    contents: list[MediaContent]
    timestamp: int | None
    url: str | None
    author: Author | None
    extra: dict[str, Any]
    repost: ParseResult | None
