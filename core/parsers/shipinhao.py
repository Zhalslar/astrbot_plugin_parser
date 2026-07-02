import re
from random import choice
from time import time
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError

from ..config import PluginConfig
from ..data import MediaContent
from ..download import Downloader
from .base import BaseParser, ParseException, Platform, handle


class ShipinhaoParser(BaseParser):
    """微信视频号解析器

    纯服务端解析思路（无需 MITM / WASM 解密）：
    1. 把分享短链 (weixin.qq.com/sph/xxx) POST 给腾讯元宝的解析接口，
       换取带 token + eid 的 playable_url（需登录态 Cookie）；
    2. 用 token + eid 调 finder-preview 的 get_feed_info 接口，
       拿到明文可播的视频直链 (h264VideoInfo.videoUrl)。

    浏览器复制的长链 (channels.weixin.qq.com/.../feed?...&eid=...&token=...)
    本身已带 token + eid，可直接走第 2 步，无需元宝 Cookie。
    """

    # 平台信息
    platform: ClassVar[Platform] = Platform(name="shipinhao", display_name="微信视频号")

    # 腾讯元宝：视频号分享链接解析接口（借其登录态签发合法 token）
    PARSE_URL = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
    # 视频号网页预览：拿到带 token 后返回真实视频直链
    FEED_INFO_URL = (
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.shipinhao
        # 元宝登录 Cookie（原始 Cookie 串，形如 "hy_source=web; hy_user=...; ..."）
        self.yuanbao_cookie = (self.mycfg.cookies or "").strip()

        # 元宝解析接口所需请求头
        self.yb_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://yuanbao.tencent.com",
            "referer": "https://yuanbao.tencent.com/chat",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "x-language": "zh-CN",
            "x-platform": "mac",
            "x-requested-with": "XMLHttpRequest",
            "x-source": "web",
        }
        # 下载视频号 CDN 资源的请求头
        self.media_headers = {
            **self.headers,
            "referer": "https://channels.weixin.qq.com/",
        }

    # https://weixin.qq.com/sph/AuCZlx1A3C  （App 复制的短链）
    @handle("weixin.qq.com/sph", r"weixin\.qq\.com/sph/[A-Za-z0-9]+")
    # https://channels.weixin.qq.com/finder-preview/pages/sph?id=xxx  （跳转 / 浏览器长链）
    @handle(
        "finder-preview",
        r"channels\.weixin\.qq\.com/finder-preview/pages/[A-Za-z0-9_]+\?[A-Za-z0-9._=&%/+\-]+",
    )
    async def _parse(self, searched: re.Match[str]):
        share_url = f"https://{searched.group(0)}"

        # 长链自带 token + eid 时，直接走 get_feed_info，无需元宝
        token, export_id = self._extract_token_eid(share_url)
        if not (token and export_id):
            token, export_id = await self._parse_share_url(share_url)

        feed = await self._get_feed_info(export_id, token)
        return self._build_result(feed, share_url)

    # ---------------- Step 1: 元宝换取 token + eid ----------------

    async def _parse_share_url(self, share_url: str) -> tuple[str, str]:
        if not self.yuanbao_cookie:
            raise ParseException(
                "微信视频号解析需要配置「腾讯元宝」Cookie：\n"
                "登录 yuanbao.tencent.com 后复制 Cookie 填入插件配置的视频号解析器中。"
            )

        payload = {"type": "video_channel_url", "url": share_url, "scene": 1}
        headers = {**self.yb_headers, "cookie": self.yuanbao_cookie}

        async with self.session.post(
            self.PARSE_URL, json=payload, headers=headers
        ) as resp:
            if resp.status == 401:
                raise ParseException("元宝 Cookie 已失效，请重新登录并更新 Cookie")
            if resp.status >= 400:
                raise ClientError(f"元宝解析接口 {resp.status} {resp.reason}")
            data: dict[str, Any] = await resp.json()

        result = data.get("data") or {}
        playable_url = result.get("playable_url") or ""
        token, export_id = self._extract_token_eid(playable_url)

        # 兜底：playable_url 里没取到 eid 时，用返回的 wx_export_id
        export_id = export_id or result.get("wx_export_id") or ""

        if not export_id:
            raise ParseException("元宝未返回视频号 eid，可能是链接失效或非视频内容")
        return token, export_id

    # ---------------- Step 2: get_feed_info 拿视频直链 ----------------

    async def _get_feed_info(self, export_id: str, token: str) -> dict[str, Any]:
        rid = f"{int(time()):x}-{self._rand_hex(8)}"
        api_url = (
            f"{self.FEED_INFO_URL}?_rid={rid}"
            "&_pageUrl=https%3A%2F%2Fchannels.weixin.qq.com"
            "%2Ffinder-preview%2Fpages%2Ffeed"
        )
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed"
            f"?entry_card_type=48&comment_scene=39&appid=0&token={token}"
            f"&entry_scene=0&eid={export_id}"
        )
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://channels.weixin.qq.com",
            "referer": referer,
            "user-agent": self.yb_headers["user-agent"],
        }
        payload = {"baseReq": {"generalToken": token}, "exportId": export_id}

        async with self.session.post(
            api_url, json=payload, headers=headers
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"视频号预览接口 {resp.status} {resp.reason}")
            data: dict[str, Any] = await resp.json()

        if data.get("errCode") not in (0, None):
            raise ParseException(f"视频号预览接口返回错误: {data.get('errMsg')}")
        return data

    # ---------------- 组装解析结果 ----------------

    def _build_result(self, feed: dict[str, Any], share_url: str):
        data = feed.get("data") or {}
        feed_info: dict[str, Any] = data.get("feedInfo") or {}
        author_info: dict[str, Any] = data.get("authorInfo") or {}

        err = feed_info.get("errMsg") or {}
        if err.get("type"):
            raise ParseException(err.get("title") or "该视频号内容无法解析")

        video_url = self._pick_video_url(feed_info)
        if not video_url:
            raise ParseException(
                "未获取到视频直链（可能是图文动态、已删除，或 Cookie 权限不足）"
            )

        cover_url = feed_info.get("coverUrl") or None
        duration = self._pick_duration(feed_info)

        contents: list[MediaContent] = [
            self.create_video_content(
                video_url,
                cover_url,
                duration,
                headers=self.media_headers,
            )
        ]

        author = self.create_author(
            author_info.get("nickname") or "视频号用户",
            author_info.get("headImgUrl") or None,
            headers=self.media_headers,
        )

        return self.result(
            title=feed_info.get("description") or None,
            author=author,
            contents=contents,
            timestamp=self._safe_int(feed_info.get("createtime")),
            url=share_url,
            extra={"info": self._build_stats(feed_info)},
        )

    # ---------------- 辅助 ----------------

    @staticmethod
    def _extract_token_eid(url: str) -> tuple[str, str]:
        """从 URL 的 query 中提取 token 与 eid，取不到返回空串"""
        if not url:
            return "", ""
        try:
            qs = parse_qs(urlparse(url).query)
        except ValueError:
            return "", ""
        token = (qs.get("token") or [""])[0]
        eid = (qs.get("eid") or qs.get("exportId") or [""])[0]
        return token, eid

    @staticmethod
    def _pick_video_url(feed_info: dict[str, Any]) -> str | None:
        for key in ("h264VideoInfo", "h265VideoInfo"):
            info = feed_info.get(key)
            if isinstance(info, dict) and info.get("videoUrl"):
                return info["videoUrl"]
        return feed_info.get("videoUrl") or None

    @staticmethod
    def _pick_duration(feed_info: dict[str, Any]) -> float:
        for key in ("h264VideoInfo", "h265VideoInfo"):
            info = feed_info.get(key)
            if isinstance(info, dict) and info.get("duration"):
                return ShipinhaoParser._safe_int(info.get("duration")) or 0.0
        return ShipinhaoParser._safe_int(feed_info.get("videoDuration")) or 0.0

    @staticmethod
    def _build_stats(feed_info: dict[str, Any]) -> str | None:
        pairs = (
            ("赞", feed_info.get("likeCountFmt")),
            ("爱心", feed_info.get("favCountFmt")),
            ("评论", feed_info.get("commentCountFmt")),
            ("转发", feed_info.get("forwardCountFmt")),
        )
        tokens = [f"{label} {value}" for label, value in pairs if value]
        return " · ".join(tokens) if tokens else None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _rand_hex(length: int) -> str:
        chars = "0123456789abcdef"
        return "".join(choice(chars) for _ in range(length))
