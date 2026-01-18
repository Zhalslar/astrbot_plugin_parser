
"""
from re import Match
from typing import ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import Platform
from ..download import Downloader
from .base import BaseParser, handle


#这是一个示例解析器，请感兴趣的开发者自行实现解析器，并提交PR。


class ExampleParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="example", display_name="示例网站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.example

    @handle("ex.short", r"ex\.short/\w+)")
    async def _parse_short_link(self, searched: Match[str]):
        url = f"https://{searched.group(0)}"
        # 重定向再解析，请确保重定向链接的 handle 存在
        # 比如 url 重定向到 example.com/... 就会调用 _parse 解析
        return await self.parse_with_redirect(url)

    @handle("example.com", r"example\.com/video/(?P<video_id>\w+)")
    @handle("exam.ple", r"exam\.ple/(?P<video_id>\w+)")
    async def _parse(self, searched: Match[str]):
        # 1. 提取视频 ID
        video_id = searched.group("video_id")
        url = f"https://api.example.com/video/{video_id}"
        # 2. 请求 API 获取视频信息
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"HTTP {resp.status} {resp.reason}")
            data = await resp.json()

        # 3. 提取数据
        title = data["title"]
        author_name = data["author"]["name"]
        avatar_url = data["author"]["avatar"]
        video_url = data["video_url"]
        cover_url = data["cover_url"]
        duration = data["duration"]
        timestamp = data["publish_time"]
        description = data.get("description", "")

        # 4. 视频内容
        author = self.create_author(author_name, avatar_url)
        video = self.create_video_content(video_url, cover_url, duration)

        # 5. 图集内容
        image_urls = data.get("images")
        images = self.create_image_contents(image_urls)

        # 6. 返回解析结果
        return self.result(
            title=title,
            text=description,
            author=author,
            contents=[video, *images],
            timestamp=timestamp,
            url=f"https://example.com/video/{video_id}",
        )



# 构建作者信息

author = self.create_author(
    name="作者名",
    avatar_url="https://example.com/avatar.jpg",   # 可选，会自动下载
    description="个性签名"                          # 可选
)


# 构建视频内容

## 方式1：传入 URL，自动下载
video = self.create_video_content(
    url_or_task="https://example.com/video.mp4",
    cover_url="https://example.com/cover.jpg",  # 可选
    duration=120.5                               # 可选，单位：秒
)

## 方式2：传入已创建的下载任务
video_task = self.download.download_video(url, ext_headers=self.headers)
video = self.create_video_content(
    url_or_task=video_task,
    cover_url=cover_url,
    duration=duration
)


# 并发下载图集内容
images = self.create_image_contents([
    "https://example.com/img1.jpg",
    "https://example.com/img2.jpg",
])


# 构建图文内容(适用于类似 Bilibili 动态图文混排)

graphics = self.create_graphics_content(
    image_url="https://example.com/image.jpg",
    text="图片前的文字说明",  # 可选
    alt="图片描述"            # 可选，居中显示
)


# 创建动图GIF内容，平台一般只提供视频, 后续插件会做自动转为 gif 的处理

dynamics = self.create_dynamic_contents([
    "https://example.com/dynamic1.mp4",
    "https://example.com/dynamic2.mp4",
])


# 重定向 url

real_url = await self.get_redirect_url(
    url="https://short.url/abc",
    headers=self.headers  # 可选
)

"""
