import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import msgspec

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    handle,
)

if TYPE_CHECKING:
    from ..data import ParseResult


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.douyin_ck = config.get("douyin_ck", "")
        self._cookies_file = Path(config["data_dir"]) / "douyin_cookies.json"
        self._load_cookies()
        if self.douyin_ck:
            self.ios_headers["Cookie"] = self.douyin_ck
            self.android_headers["Cookie"] = self.douyin_ck

    def _load_cookies(self):
        """从文件加载抖音 cookies"""
        if not self._cookies_file.exists():
            return

        try:
            cookies_data = json.loads(self._cookies_file.read_text())
            self.douyin_ck = cookies_data.get("cookie", "")
            if self.douyin_ck:
                self.ios_headers["Cookie"] = self.douyin_ck
                self.android_headers["Cookie"] = self.douyin_ck
                logger.info(f"已从 {self._cookies_file} 加载抖音 cookies")
        except Exception as e:
            logger.warning(f"加载抖音 cookies 失败: {e}")

    def _save_cookies(self, cookies: str):
        """保存抖音 cookies 到文件"""
        try:
            self._cookies_file.write_text(json.dumps({"cookie": cookies}, ensure_ascii=False))
            logger.info(f"已保存抖音 cookies 到 {self._cookies_file}")
        except Exception as e:
            logger.warning(f"保存抖音 cookies 失败: {e}")

    def _update_cookies_from_response(self, set_cookie_headers: list[str]):
        """从响应的 Set-Cookie 头中更新 cookies"""
        if not set_cookie_headers:
            return

        # 解析现有的 cookies
        existing_cookies = {}
        if self.douyin_ck:
            for cookie in self.douyin_ck.split(";"):
                cookie = cookie.strip()
                if cookie and "=" in cookie:
                    name, value = cookie.split("=", 1)
                    existing_cookies[name.strip()] = value.strip()

        # 解析新的 cookies
        for set_cookie in set_cookie_headers:
            cookie_part = set_cookie.split(";")[0].strip()
            if cookie_part and "=" in cookie_part:
                name, value = cookie_part.split("=", 1)
                existing_cookies[name.strip()] = value.strip()

        # 合并为 cookie 字符串
        new_cookies = "; ".join([f"{k}={v}" for k, v in existing_cookies.items()])

        if new_cookies != self.douyin_ck:
            self.douyin_ck = new_cookies
            self.ios_headers["Cookie"] = self.douyin_ck
            self.android_headers["Cookie"] = self.douyin_ck
            self._save_cookies(self.douyin_ck)
    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")
        if ty == "slides":
            return await self.parse_slides(vid)

        for url in (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        ):
            try:
                return await self.parse_video(url)
            except ParseException as e:
                logger.warning(f"failed to parse {url}, error: {e}")
                continue
        raise ParseException("分享已删除或资源直链提取失败, 请稍后再试")

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> "ParseResult":
        """先重定向再解析，并更新 cookies"""
        headers = headers or self.ios_headers
        async with self.client.get(
            url, headers=headers, allow_redirects=False, ssl=False
        ) as resp:
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                self._update_cookies_from_response(set_cookie_headers)

            # 只有在状态码是重定向状态码时才获取 Location
            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str):
        async with self.client.get(
            url, headers=self.ios_headers, allow_redirects=False, ssl=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                self._update_cookies_from_response(set_cookie_headers)

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        from .video import RouterData

        video_data = msgspec.json.decode(matched.group(1).strip(), type=RouterData).video_data
        # 使用新的简洁构建方式
        contents = []

        # 添加图片内容
        if image_urls := video_data.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        # 添加视频内容
        elif video_url := video_data.video_url:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            contents.append(self.create_video_content(video_url, cover_url, duration))

        # 构建作者
        author = self.create_author(video_data.author.nickname, video_data.avatar_url)

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
        )

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        async with self.client.get(
            url, params=params, headers=self.android_headers, ssl=False
        ) as resp:
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                self._update_cookies_from_response(set_cookie_headers)

            from .slides import SlidesInfo

            slides_data = msgspec.json.decode(await resp.read(), type=SlidesInfo).aweme_details[0]
        contents = []

        # 添加图片内容
        if image_urls := slides_data.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        # 添加动态内容
        if dynamic_urls := slides_data.dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        # 构建作者
        author = self.create_author(slides_data.name, slides_data.avatar_url)

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )
