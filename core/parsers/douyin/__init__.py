
import re
from typing import TYPE_CHECKING, ClassVar

import msgspec

from astrbot.api import logger

from ...config import PluginConfig
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    handle,
)

if TYPE_CHECKING:
    from ...data import ParseResult


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.douyin
        self.cookies = self.mycfg.cookies
        if self.cookies:
            self._set_cookies(self.cookies)
    def _set_cookies(self, cookies: str):
        """设置cookie到请求头"""
        cleaned_cookies = cookies.replace("\n", "").replace("\r", "").strip()
        if cleaned_cookies:
            self.ios_headers["Cookie"] = cleaned_cookies
            self.android_headers["Cookie"] = cleaned_cookies

    def _update_cookies_from_response(self, set_cookie_headers: list[str]):
        """从响应的 Set-Cookie 头中更新 cookies"""
        if not set_cookie_headers:
            return

        logger.debug(f"[抖音] 开始更新 cookies，收到 {len(set_cookie_headers)} 个 Set-Cookie")

        # 解析现有的 cookies
        existing_cookies = {}
        if self.cookies:
            for cookie in self.cookies.split(";"):
                cookie = cookie.strip()
                if cookie and "=" in cookie:
                    name, value = cookie.split("=", 1)
                    existing_cookies[name.strip()] = value.strip()
            logger.debug(f"[抖音] 现有 cookies 数量: {len(existing_cookies)}")

        # 解析新的 cookies
        new_cookie_names = []
        for set_cookie in set_cookie_headers:
            cookie_part = set_cookie.split(";")[0].strip()
            if cookie_part and "=" in cookie_part:
                name, value = cookie_part.split("=", 1)
                existing_cookies[name.strip()] = value.strip()
                new_cookie_names.append(name.strip())

        logger.debug(f"[抖音] 新增/更新的 cookies: {new_cookie_names}")

        # 合并为 cookie 字符串
        new_cookies = "; ".join([f"{k}={v}" for k, v in existing_cookies.items()])

        if new_cookies != self.cookies:
            self.cookies = new_cookies
            self.cfg.save()
            self._set_cookies(self.cookies)
            logger.debug("[抖音] Cookies 已更新并保存")
        else:
            logger.debug("[抖音] Cookies 无变化")
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
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        if ty == "slides":
            return await self.parse_slides(vid)

        urls = (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        )
        logger.debug(f"[抖音] 尝试解析URL列表: {urls}")

        for url in urls:
            try:
                logger.debug(f"[抖音] 尝试解析: {url}")
                return await self.parse_video(url)
            except ParseException as e:
                logger.warning(f"[抖音] 解析失败 {url}, 错误: {e}")
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
        logger.debug(f"[抖音] 短链重定向请求: {url}")
        logger.debug(f"[抖音] 请求头 User-Agent: {headers.get('User-Agent', 'N/A')}")
        logger.debug(f"[抖音] 请求头 Cookie: {'已配置' if headers.get('Cookie') else '未配置'}")

        async with self.session.get(
            url, headers=headers, allow_redirects=False, ssl=False
        ) as resp:
            logger.debug(f"[抖音] 短链重定向响应状态码: {resp.status}")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                logger.debug(f"[抖音] 收到 {len(set_cookie_headers)} 个 Set-Cookie")
                self._update_cookies_from_response(set_cookie_headers)

            # 只有在状态码是重定向状态码时才获取 Location
            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)
                logger.debug(f"[抖音] 重定向到: {redirect_url}")

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str):
        logger.debug(f"[抖音] 视频页面请求: {url}")
        logger.debug(f"[抖音] 请求头 User-Agent: {self.ios_headers.get('User-Agent', 'N/A')}")
        logger.debug(f"[抖音] 请求头 Cookie: {'已配置' if self.ios_headers.get('Cookie') else '未配置'}")

        async with self.session.get(
            url, headers=self.ios_headers, allow_redirects=False, ssl=False
        ) as resp:
            logger.debug(f"[抖音] 视频页面响应状态码: {resp.status}")
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            logger.debug(f"[抖音] 响应体大小: {len(text)} 字符")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                logger.debug(f"[抖音] 收到 {len(set_cookie_headers)} 个 Set-Cookie")
                self._update_cookies_from_response(set_cookie_headers)

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            logger.debug("[抖音] 未在HTML中找到 window._ROUTER_DATA")
            raise ParseException("can't find _ROUTER_DATA in html")

        logger.debug("[抖音] 成功提取 window._ROUTER_DATA")

        from .video import RouterData

        video_data = msgspec.json.decode(matched.group(1).strip(), type=RouterData).video_data
        logger.debug(f"[抖音] 解析成功 - 作者: {video_data.author.nickname}, 描述: {video_data.desc[:50]}...")
        # 使用新的简洁构建方式
        contents = []

        # 添加图片内容
        if image_urls := video_data.image_urls:
            logger.debug(f"[抖音] 检测到图文内容，图片数量: {len(image_urls)}")
            contents.extend(self.create_image_contents(image_urls, headers=self.ios_headers))

        # 添加视频内容
        elif video_url := video_data.video_url:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            logger.debug(f"[抖音] 检测到视频内容，时长: {duration}秒")
            contents.append(self.create_video_content(video_url, cover_url, duration, headers=self.ios_headers))

        # 构建作者
        author = self.create_author(video_data.author.nickname, video_data.avatar_url, headers=self.ios_headers)

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
        logger.debug(f"[抖音] 幻灯片API请求: {url}")
        logger.debug(f"[抖音] 请求参数: {params}")
        logger.debug(f"[抖音] 请求头 User-Agent: {self.android_headers.get('User-Agent', 'N/A')}")
        logger.debug(f"[抖音] 请求头 Cookie: {'已配置' if self.android_headers.get('Cookie') else '未配置'}")

        async with self.session.get(
            url, params=params, headers=self.android_headers, ssl=False
        ) as resp:
            logger.debug(f"[抖音] 幻灯片API响应状态码: {resp.status}")
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                logger.debug(f"[抖音] 收到 {len(set_cookie_headers)} 个 Set-Cookie")
                self._update_cookies_from_response(set_cookie_headers)

            from .slides import SlidesInfo

            response_text = await resp.read()
            logger.debug(f"[抖音] 幻灯片API响应体大小: {len(response_text)} 字节")
            slides_data = msgspec.json.decode(response_text, type=SlidesInfo).aweme_details[0]
        logger.debug(f"[抖音] 幻灯片解析成功 - 作者: {slides_data.name}, 描述: {slides_data.desc[:50]}...")
        contents = []

        # 添加图片内容
        if image_urls := slides_data.image_urls:
            logger.debug(f"[抖音] 检测到幻灯片图片，数量: {len(image_urls)}")
            contents.extend(self.create_image_contents(image_urls, headers=self.android_headers))

        # 添加动态内容
        if dynamic_urls := slides_data.dynamic_urls:
            logger.debug(f"[抖音] 检测到幻灯片动态效果，数量: {len(dynamic_urls)}")
            contents.extend(self.create_dynamic_contents(dynamic_urls, headers=self.android_headers))

        # 构建作者
        author = self.create_author(slides_data.name, slides_data.avatar_url, headers=self.android_headers)

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )
