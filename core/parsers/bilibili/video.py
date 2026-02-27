from dataclasses import dataclass

from msgspec import Struct

from .common import Upper


class Stats(Struct):
    view: int
    """播放量"""
    danmaku: int
    """弹幕数"""
    reply: int
    """回复数"""
    favorite: int
    """收藏数"""
    coin: int
    """硬币数"""
    share: int
    """分享数"""
    like: int
    """点赞数"""


class Page(Struct):
    part: str
    """分集标题"""
    ctime: int
    """创建时间戳"""
    duration: int
    """时长"""
    first_frame: str | None = None
    """封面图片"""


@dataclass(frozen=True, slots=True)
class PageInfo:
    index: int
    title: str
    duration: int
    timestamp: int
    cover: str | None = None


class VideoInfo(Struct):
    bvid: str
    """bvid"""
    title: str
    """标题"""
    desc: str
    """简介"""
    duration: int
    """时长"""
    owner: Upper
    """作者信息"""
    stat: Stats
    """统计信息"""
    pubdate: int
    """公开时间戳"""
    ctime: int
    """创建时间戳"""
    pic: str | None = None
    """封面图片"""
    pages: list[Page] | None = None
    """分集信息"""

    @property
    def title_with_part(self) -> str:
        if self.pages and len(self.pages) > 1:
            return f"{self.title} - {self.pages[0].part}"
        return self.title

    @property
    def formatted_stats_info(self) -> str:
        """
        格式化视频信息
        """
        def format_duration(seconds: int) -> str:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            if hours > 0:
                return f"{hours}:{minutes:02d}:{secs:02d}"
            return f"{minutes}:{secs:02d}"

        # 定义需要展示的数据及其显示名称
        stats_mapping = [
            ("⏳", format_duration(self.duration)),
            ("👍", self.stat.like),
            ("🪙", self.stat.coin),
            ("⭐", self.stat.favorite),
            ("↩️", self.stat.share),
            ("💬", self.stat.reply),
            ("👀", self.stat.view),
            ("💭", self.stat.danmaku),
        ]

        # 构建结果字符串
        result_parts = []
        for display_name, value in stats_mapping:
            if isinstance(value, str):
                result_parts.append(f"{display_name} {value}")
                continue
            # 数值超过10000时转换为万为单位
            if value > 10000:
                formatted_value = f"{value / 10000:.1f}万"
                result_parts.append(f"{display_name} {formatted_value} ({value})")
            else:
                result_parts.append(f"{display_name} {value}")

        return " ".join(result_parts)

    def extract_info_with_page(self, page_num: int = 1) -> PageInfo:
        """获取视频信息，包含页索引、标题、时长、封面
        Args:
            page_num (int): 页索引. Defaults to 1.

        Returns:
            tuple[int, str, int, str | None]: 页索引、标题、时长、封面
        """
        page_idx = page_num - 1
        title = self.title
        duration = self.duration
        cover = self.pic
        timestamp = self.pubdate

        if self.pages and len(self.pages) > 1:
            page_idx = page_idx % len(self.pages)
            page = self.pages[page_idx]
            title += f" | 分集 - {page.part}"
            duration = page.duration
            cover = page.first_frame
            timestamp = page.ctime

        return PageInfo(
            index=page_idx,
            title=title,
            duration=duration,
            timestamp=timestamp,
            cover=cover,
        )


class ModelResult(Struct):
    summary: str


class AIConclusion(Struct):
    model_result: ModelResult | None = None

    @property
    def summary(self) -> str:
        if self.model_result and self.model_result.summary:
            return f"AI总结: {self.model_result.summary}"
        return "该视频暂不支持AI总结"
