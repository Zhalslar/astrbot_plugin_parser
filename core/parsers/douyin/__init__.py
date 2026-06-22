import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    handle,
)

if TYPE_CHECKING:
    from ...data import ParseResult


@dataclass(slots=True)
class ProbedVideo:
    url: str
    size: int
    headers: dict[str, str]


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")
    PLAY_RATIOS: ClassVar[tuple[str, ...]] = ("1080p", "720p", "540p", "360p")
    TTWID_REGISTER_URL: ClassVar[str] = (
        "https://ttwid.bytedance.com/ttwid/union/register/"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.douyin
        self.cookiejar = CookieJar(config, self.mycfg, domain="douyin.com")
        self._set_cookies()

    def _set_cookies(self, cookies_str: str = ""):
        """设置cookie到请求头"""
        cookies_str = cookies_str or self.cookiejar.cookies_str
        if cookies_str:
            self.ios_headers["Cookie"] = cookies_str
            self.android_headers["Cookie"] = cookies_str

    def _sync_headers_for_url(self, url: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        if cookies_str := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookies_str
        elif self._is_iesdouyin_url(url):
            if cookies_str := self.cookiejar.get_cookie_header(domain="iesdouyin.com"):
                headers["Cookie"] = cookies_str
        return headers

    @staticmethod
    def _is_iesdouyin_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")

    def _has_ttwid(self) -> bool:
        cookies = self.cookiejar.get(domain="iesdouyin.com") or {}
        return bool(cookies.get("ttwid"))

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("", r"(?<!\d)(?P<vid>\d{18,20})(?!\d)")
    @handle("aweme_id", r"aweme_id[=:/\s]+(?P<vid>\d{10,})")
    @handle("aweme", r"aweme/(?P<vid>\d{10,})")
    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty = searched.groupdict().get("ty") or "video"
        vid = searched.group("vid")
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        if ty == "slides":
            return await self.parse_slides(vid)

        await self.ensure_ttwid()
        share_url = self._build_iesdouyin_url(ty, vid)
        logger.debug(f"[抖音] 使用 canonical share 页解析: {share_url}")

        try:
            return await self.parse_video(share_url)
        except ParseException as e:
            logger.warning(f"[抖音] canonical share 页解析失败 {share_url}, 错误: {e}")
            raise ParseException("分享已删除或资源直链提取失败, 请稍后再试") from e

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}/"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}/"

    async def ensure_ttwid(self) -> None:
        if self._has_ttwid():
            return

        logger.debug("[抖音] 当前缺少匿名 ttwid，尝试注册")
        headers = self.ios_headers.copy()
        headers.update(
            {
                "Content-Type": "application/json",
                "Referer": "https://www.iesdouyin.com/",
            }
        )
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        }
        try:
            async with self.session.post(
                self.TTWID_REGISTER_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"ttwid register status: {resp.status}")
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(set_cookie_headers)
                self._set_cookies()
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise ParseException("ttwid register failed") from e

        if not isinstance(body, dict):
            raise ParseException("ttwid register returned invalid body")

        if callback_url := body.get("redirect_url"):
            callback_headers = self._sync_headers_for_url(callback_url)
            callback_headers["Referer"] = "https://www.iesdouyin.com/"
            try:
                async with self.session.get(
                    callback_url,
                    headers=callback_headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException(f"ttwid callback status: {resp.status}")
                    set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                    self.cookiejar.update_from_response(set_cookie_headers)
                    self._set_cookies()
            except (ClientError, TimeoutError) as e:
                raise ParseException("ttwid callback failed") from e

        if not self._has_ttwid():
            raise ParseException("ttwid register returned no cookie")

    async def parse_with_redirect(self, url: str) -> "ParseResult":
        """先重定向再解析，并更新 cookies"""
        logger.debug(f"[抖音] 短链重定向请求: {url}")
        async with self.session.get(
            url, headers=self.ios_headers, allow_redirects=False
        ) as resp:
            logger.debug(f"[抖音] 短链重定向响应状态码: {resp.status}")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

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
        await self.ensure_ttwid()
        share_headers = self._sync_headers_for_url(url)
        async with self.session.get(
            url, headers=share_headers, allow_redirects=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

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

        video_data = msgspec.json.decode(
            matched.group(1).strip(), type=RouterData
        ).video_data
        logger.debug(
            f"[抖音] 解析成功 - 作者: {video_data.author.nickname}, 描述: {video_data.desc[:50]}..."
        )
        # 使用新的简洁构建方式
        contents = []

        # 添加图片内容
        if image_urls := video_data.image_urls:
            logger.debug(f"[抖音] 检测到图文内容，图片数量: {len(image_urls)}")
            contents.extend(
                self.create_image_contents(image_urls, headers=self.ios_headers)
            )

        # 添加视频内容
        elif video_data.video:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            logger.debug(f"[抖音] 检测到视频内容，时长: {duration}秒")
            video_headers = self._build_media_headers(url)
            video_url = None
            if play_token := video_data.play_token:
                try:
                    probed = await self.probe_video_url(play_token, url)
                    video_url = probed.url
                    video_headers = probed.headers
                    logger.debug(
                        f"[抖音] play 端点探测成功，文件大小: {probed.size} 字节"
                    )
                except ParseException as e:
                    logger.warning(f"[抖音] play 端点探测失败，回退 play_addr: {e}")
            video_url = video_url or video_data.video_url
            if video_url:
                contents.append(
                    self.create_video_content(
                        video_url, cover_url, duration, headers=video_headers
                    )
                )

        # 构建作者
        author = self.create_author(
            video_data.author.nickname, video_data.avatar_url, headers=self.ios_headers
        )

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
        )

    @staticmethod
    def _build_play_url(video_id: str, ratio: str) -> str:
        return (
            "https://aweme.snssdk.com/aweme/v1/play/"
            f"?video_id={video_id}&ratio={ratio}"
        )

    def _build_media_headers(self, referer: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        headers["Referer"] = referer
        return headers

    async def probe_video_url(self, video_id: str, referer: str) -> ProbedVideo:
        probed_by_size: dict[int, ProbedVideo] = {}

        for ratio in self.PLAY_RATIOS:
            play_url = self._build_play_url(video_id, ratio)
            headers = self._build_media_headers(referer)
            headers["Range"] = "bytes=0-1"
            try:
                async with self.session.get(
                    play_url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status >= 400:
                        logger.debug(
                            f"[抖音] ratio={ratio} 探测失败，状态码: {resp.status}"
                        )
                        continue
                    size = self._extract_response_size(resp.headers)
                    if size <= 0:
                        logger.debug(f"[抖音] ratio={ratio} 未拿到有效文件大小")
                        continue
                    final_url = str(resp.url)
            except (ClientError, TimeoutError) as e:
                logger.debug(f"[抖音] ratio={ratio} 探测请求失败: {e}")
                continue

            probed_by_size.setdefault(
                size, ProbedVideo(final_url, size, self._build_media_headers(referer))
            )

        if not probed_by_size:
            raise ParseException("can't probe play endpoint")

        return max(probed_by_size.values(), key=lambda item: item.size)

    @staticmethod
    def _extract_response_size(headers) -> int:
        if content_range := headers.get("Content-Range"):
            if matched := re.search(r"/(\d+)\s*$", content_range):
                return int(matched.group(1))
        if content_length := headers.get("Content-Length"):
            try:
                return int(content_length)
            except ValueError:
                return 0
        return 0

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        logger.debug(f"[抖音] 请求参数: {params}")
        async with self.session.get(
            url, params=params, headers=self.android_headers
        ) as resp:
            logger.debug(f"[抖音] 幻灯片API响应状态码: {resp.status}")
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            from .slides import SlidesInfo

            response_text = await resp.read()
            logger.debug(f"[抖音] 幻灯片API响应体大小: {len(response_text)} 字节")
            slides_data = msgspec.json.decode(
                response_text, type=SlidesInfo
            ).aweme_details[0]
        logger.debug(
            f"[抖音] 幻灯片解析成功 - 作者: {slides_data.name}, 描述: {slides_data.desc[:50]}..."
        )
        contents = []

        # 添加图片内容
        if image_urls := slides_data.image_urls:
            logger.debug(f"[抖音] 检测到幻灯片图片，数量: {len(image_urls)}")
            contents.extend(
                self.create_image_contents(image_urls, headers=self.android_headers)
            )

        # 添加动态内容
        if dynamic_urls := slides_data.dynamic_urls:
            logger.debug(f"[抖音] 检测到幻灯片动态效果，数量: {len(dynamic_urls)}")
            contents.extend(
                self.create_dynamic_contents(dynamic_urls, headers=self.android_headers)
            )

        # 构建作者
        author = self.create_author(
            slides_data.name, slides_data.avatar_url, headers=self.android_headers
        )

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )
