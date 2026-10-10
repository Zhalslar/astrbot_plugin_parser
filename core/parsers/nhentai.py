"""nhentai 解析器

流程：画廊接口 → 元数据/页面清单 → 下载全部页面 → 合成 PDF 发送
"""

import asyncio
from pathlib import Path
from re import Match
from typing import Any, ClassVar

from aiohttp import ClientError
from PIL import Image, ImageFilter

from astrbot.api import logger

from ..config import PluginConfig
from ..data import (
    FileContent,
    ImageContent,
    MediaContent,
    ParseResult,
    Platform,
    SendGroup,
)
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle

NH_BASE = "https://nhentai.net"
"""画廊站点，同时提供 API v2"""
NH_API_GALLERY = f"{NH_BASE}/api/v2/galleries"
NH_API_CDN = f"{NH_BASE}/api/v2/cdn"

NH_DEFAULT_IMAGE_SERVERS: tuple[str, ...] = (
    "https://i1.nhentai.net",
    "https://i2.nhentai.net",
    "https://i3.nhentai.net",
    "https://i4.nhentai.net",
)
"""正文页图源，接口不可用时的兜底"""
NH_DEFAULT_THUMB_SERVERS: tuple[str, ...] = (
    "https://t1.nhentai.net",
    "https://t2.nhentai.net",
    "https://t3.nhentai.net",
    "https://t4.nhentai.net",
)
"""封面/缩略图图源，接口不可用时的兜底"""

NH_HEADER: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{NH_BASE}/",
}

NH_TAG_LABELS: tuple[tuple[str, str], ...] = (
    ("artist", "作者"),
    ("group", "社团"),
    ("parody", "原作"),
    ("character", "角色"),
    ("category", "分类"),
    ("language", "语言"),
)
"""标签分类 → 卡片展示名称（顺序即展示顺序）"""


def imgs_to_pdf(img_paths: list[Path], save_path: Path) -> Path:
    """把按页码排好序的图片合成为 PDF"""
    if not img_paths:
        raise ParseException("图片列表为空")

    images: list[Image.Image] = []
    try:
        for img_path in img_paths:
            with Image.open(img_path) as img:
                images.append(img.convert("RGB"))
        images[0].save(save_path, "PDF", save_all=True, append_images=images[1:])
    finally:
        for img in images:
            img.close()
    return save_path


def blur_image(image_path: Path, radius: int = 25) -> Path:
    """对图片施加高斯模糊，返回模糊后的新文件"""
    output_path = image_path.parent / f"{image_path.stem}_blur{image_path.suffix}"
    with Image.open(image_path) as img:
        img.filter(ImageFilter.GaussianBlur(radius=radius)).save(output_path)
    return output_path


def group_tags(tags: list[dict[str, Any]]) -> dict[str, list[str]]:
    """按标签类型分组，保持接口返回的顺序"""
    grouped: dict[str, list[str]] = {}
    for tag in tags:
        name = str(tag.get("name") or "").strip()
        if not name:
            continue
        grouped.setdefault(str(tag.get("type") or "tag"), []).append(name)
    return grouped


def pick_title(data: dict[str, Any]) -> str:
    """优先取包含译名的 pretty 标题"""
    title = data.get("title")
    if isinstance(title, dict):
        for key in ("pretty", "english", "japanese"):
            if value := str(title.get(key) or "").strip():
                return value
    return f"nhentai {data.get('id', '')}".strip()


def build_text(data: dict[str, Any], grouped: dict[str, list[str]]) -> str:
    """组装卡片正文"""
    lines = [f"页数: {data.get('num_pages', 0)}"]

    for key, label in NH_TAG_LABELS:
        if names := grouped.get(key):
            lines.append(f"{label}: {', '.join(names)}")

    if names := grouped.get("tag"):
        lines.append("标签: " + ", ".join(f"#{name}" for name in names))

    if favorites := data.get("num_favorites"):
        lines.append(f"收藏: {favorites}")

    return "\n".join(lines)


class NhentaiParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="nhentai", display_name="nhentai")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.nhentai
        self._image_servers: list[str] = []
        self._thumb_servers: list[str] = []

    # ---------------- 接口 ----------------

    async def _get_json(self, url: str, *, not_found: str = "nhentai 接口不存在") -> Any:
        """请求接口并解析 JSON"""
        try:
            async with self.session.get(
                url, headers=NH_HEADER, proxy=self.proxy
            ) as resp:
                if resp.status == 404:
                    raise ParseException(not_found)
                if resp.status != 200:
                    raise ParseException(f"nhentai 接口返回 HTTP {resp.status}")
                return await resp.json(content_type=None)
        except ParseException:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ParseException(f"nhentai 接口请求失败: {exc}") from exc

    async def _get_servers(self) -> tuple[list[str], list[str]]:
        """获取正文/封面图源服务器（带实例级缓存）"""
        if self._image_servers and self._thumb_servers:
            return self._image_servers, self._thumb_servers

        image_servers: list[str] = []
        thumb_servers: list[str] = []
        try:
            data = await self._get_json(NH_API_CDN)
            if isinstance(data, dict):
                image_servers = [
                    str(url).rstrip("/") for url in data.get("image_servers") or []
                ]
                thumb_servers = [
                    str(url).rstrip("/") for url in data.get("thumb_servers") or []
                ]
        except ParseException as exc:
            logger.warning(f"nhentai 图源接口不可用，使用默认图源: {exc}")

        self._image_servers = image_servers or list(NH_DEFAULT_IMAGE_SERVERS)
        self._thumb_servers = thumb_servers or list(NH_DEFAULT_THUMB_SERVERS)
        return self._image_servers, self._thumb_servers

    async def _get_gallery(self, gid: str) -> dict[str, Any]:
        """获取画廊详情"""
        data = await self._get_json(
            f"{NH_API_GALLERY}/{gid}", not_found="nhentai 画廊不存在或已被删除"
        )
        if not isinstance(data, dict) or not data.get("pages"):
            raise ParseException(f"nhentai 画廊 {gid} 数据异常")
        return data

    async def _page_urls(self, data: dict[str, Any]) -> list[str]:
        """由页面清单拼出正文图链接，多图源时按页轮换分摊负载"""
        image_servers, _ = await self._get_servers()
        servers = image_servers or list(NH_DEFAULT_IMAGE_SERVERS)
        return [
            f"{servers[index % len(servers)]}/{page['path']}"
            for index, page in enumerate(data.get("pages") or [])
            if page.get("path")
        ]

    # ---------------- 内容构建 ----------------

    async def _build_cover(self, data: dict[str, Any], blur: bool) -> list[MediaContent]:
        """下载封面，失败不影响正文，blur 时模糊处理"""
        cover = data.get("cover") or data.get("thumbnail") or {}
        path = cover.get("path")
        if not path:
            return []

        _, thumb_servers = await self._get_servers()
        url = f"{thumb_servers[0]}/{path}"
        try:
            img_path = await self.downloader.download_img(
                url, headers=NH_HEADER, proxy=self.proxy
            )
        except Exception as exc:  # noqa: BLE001 封面属于可选项
            logger.warning(f"nhentai 封面下载失败: {exc}")
            return []

        if blur:
            img_path = await asyncio.to_thread(blur_image, img_path)
        return [ImageContent(img_path)]

    async def _build_pdf(self, gid: str, img_urls: list[str]) -> Path:
        """下载全部页面并合成 PDF"""
        img_paths = await self.downloader.download_imgs_without_raise(
            img_urls, headers=NH_HEADER, proxy=self.proxy
        )
        if not img_paths:
            raise ParseException("nhentai 页面图片下载失败")

        pdf_path = self.cfg.cache_dir / f"nhentai_{gid}.pdf"
        await asyncio.to_thread(imgs_to_pdf, img_paths, pdf_path)
        logger.info(f"nhentai {gid} 已合成 PDF: {pdf_path.name}（{len(img_paths)} 页）")
        return pdf_path

    # ---------------- 处理器 ----------------

    @handle("nhentai.net/g", r"nhentai\.net/g/(?P<gid>\d+)")
    async def _parse_gallery(self, searched: Match[str]) -> ParseResult:
        gid = searched.group("gid")
        data = await self._get_gallery(gid)
        url = f"{NH_BASE}/g/{gid}/"

        grouped = group_tags(data.get("tags") or [])
        title = pick_title(data)
        text = build_text(data, grouped)
        num_pages = int(data.get("num_pages") or len(data.get("pages") or []))
        upload_date = data.get("upload_date")
        timestamp = int(upload_date) if upload_date else None
        author_name = ", ".join(grouped.get("artist") or grouped.get("group") or [])
        author = self.create_author(name=author_name or "未知作者")

        nsfw = self.mycfg.nsfw or "blur"
        if nsfw == "ignore":
            return self.result(
                title=title,
                text=text,
                url=url,
                timestamp=timestamp,
                extra={"info": "🔞 nhentai 内容已按配置忽略"},
            )

        cover_contents = await self._build_cover(data, blur=nsfw == "blur")

        max_page = self.mycfg.max_page or 0
        if max_page > 0 and num_pages > max_page:
            return self.result(
                author=author,
                title=title,
                text=text,
                url=url,
                contents=cover_contents,
                send_groups=[
                    SendGroup(contents=[], render_card=True, force_merge=False),
                    # 仅卡片会被 sender 判定为“未发送”而补发纯文本，故再带一份封面
                    SendGroup(
                        contents=cover_contents, render_card=False, force_merge=False
                    ),
                ],
                timestamp=timestamp,
                extra={
                    "info": f"共 {num_pages} 页，超过最大页数 {max_page}，已跳过下载"
                },
            )

        img_urls = await self._page_urls(data)
        if not img_urls:
            raise ParseException("nhentai 未找到页面图片")

        pdf_task = asyncio.create_task(self._build_pdf(gid, img_urls))

        return self.result(
            author=author,
            title=title,
            text=text,
            url=url,
            contents=cover_contents,
            send_groups=[
                SendGroup(contents=[], render_card=True, force_merge=False),
                SendGroup(
                    contents=[FileContent(pdf_task, name=f"nhentai_{gid}.pdf")],
                    render_card=False,
                    force_merge=False,
                ),
            ],
            timestamp=timestamp,
            extra={"info": f"共 {num_pages} 页"},
        )
