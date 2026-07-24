import asyncio
import html as html_module
import re
import zipfile
from datetime import datetime
from pathlib import Path
from re import Match
from typing import Any, ClassVar

import httpx
from PIL import Image, ImageFilter

from ..config import PluginConfig
from ..constants import COMMON_HEADER
from ..cookie import CookieJar
from ..data import (
    Author,
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

PIXIV_BASE = "https://www.pixiv.net"
PIXIV_IMG_HEADERS: dict[str, str] = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": COMMON_HEADER["User-Agent"],
}


class PixivAPI:
    """Pixiv api"""

    def __init__(
        self,
        cookiejar: CookieJar,
        proxy: str | None,
        timeout: float,
    ) -> None:
        self._proxy = proxy
        self._timeout = timeout
        self._headers: dict[str, str] = {
            "Referer": "https://www.pixiv.net/",
            "User-Agent": COMMON_HEADER["User-Agent"],
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if cookiejar.cookies_str:
            self._headers["Cookie"] = cookiejar.cookies_str
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                proxy=self._proxy,
                follow_redirects=True,
                timeout=self._timeout,
                headers=self._headers,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str) -> Any:
        url = f"{PIXIV_BASE}{path}"
        try:
            resp = await self.client.get(url)
        except httpx.HTTPError as e:
            raise ParseException(f"Pixiv API 请求失败: {e}") from e
        if resp.status_code != 200:
            raise ParseException(f"Pixiv API 返回 HTTP {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            msg = data.get("message", "未知错误")
            raise ParseException(f"Pixiv API 错误: {msg}")
        return data["body"]

    async def get_raw(self, url: str) -> httpx.Response:
        """直接 GET（用于获取 HTML 页面）"""
        return await self.client.get(url)

    # ---- 作品 ----

    async def get_illust_detail(self, pid: str) -> dict[str, Any]:
        return await self._get(f"/ajax/illust/{pid}")  # type: ignore[return-value]

    async def get_pages(self, pid: str) -> list[dict[str, Any]]:
        return await self._get(f"/ajax/illust/{pid}/pages")  # type: ignore[return-value]

    async def get_ugoira_meta(self, pid: str) -> dict[str, Any]:
        return await self._get(f"/ajax/illust/{pid}/ugoira/meta")  # type: ignore[return-value]

    # ---- 用户 ----

    async def get_user_detail(self, uid: str) -> dict[str, Any]:
        return await self._get(f"/ajax/user/{uid}")  # type: ignore[return-value]

    # ---- 小说 ----

    async def get_novel_detail(self, nid: str) -> dict[str, Any]:
        return await self._get(f"/ajax/novel/{nid}")  # type: ignore[return-value]

    # ---- 系列 ----

    async def get_novel_series_total(self, series_id: str) -> int | None:
        """小说系列总话数：/ajax/novel/series/{id} → body.total"""
        try:
            body = await self._get(f"/ajax/novel/series/{series_id}")
            if isinstance(body, dict):
                total = body.get("total")
                if total is not None:
                    return int(total)
        except ParseException:
            pass
        return None

    async def get_manga_series_total(
        self, series_id: str, user_id: str
    ) -> int | None:
        """取漫画系列总话数"""
        url = f"{PIXIV_BASE}/user/{user_id}/series/{series_id}"
        try:
            resp = await self.client.get(url)
            if resp.status_code != 200:
                return None
            match = re.search(r"(\d+)部作品", resp.text)
            if match:
                return int(match.group(1))
        except httpx.HTTPError:
            pass
        return None


class PixivHelper:
    """utils"""

    # ---- 文本格式化 ----

    @staticmethod
    def clean_html(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        return html_module.unescape(text).strip()

    @staticmethod
    def format_tags(tags_data: dict[str, Any]) -> str:
        """格式化标签：#原文(翻译) 或 #原文"""
        tags = tags_data.get("tags", [])
        parts: list[str] = []
        for tag_info in tags:
            tag = tag_info.get("tag", "")
            if not tag:
                continue
            translation_obj = tag_info.get("translation")
            if isinstance(translation_obj, dict):
                translation = translation_obj.get("en", "")
            else:
                translation = translation_obj or ""
            if translation:
                parts.append(f"#{tag}({translation})")
            else:
                parts.append(f"#{tag}")
        return ", ".join(parts)

    @staticmethod
    def build_text(body: dict[str, Any]) -> str:
        description = PixivHelper.clean_html(body.get("description", ""))
        tags_text = PixivHelper.format_tags(body.get("tags", {}))
        parts: list[str] = []
        if description:
            parts.append(f"简介: {description}")
        if tags_text:
            parts.append(f"标签: {tags_text}")
        return "\n".join(parts)

    @staticmethod
    def clean_novel_text(text: str) -> str:
        text = text.replace("[newpage]", "\n\n---\n\n")
        text = re.sub(r"\[\[jumpuri:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
        text = re.sub(r"\[\[rb:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
        text = re.sub(r"\[\[jump:[^\]]*\]\]", "", text)
        return text

    @staticmethod
    def parse_timestamp(date_str: str) -> int | None:
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    # ---- 图片处理 ----

    @staticmethod
    def blur(image_path: str | Path, radius: int = 20) -> Path:
        image_path = Path(image_path)
        output_path = image_path.parent / f"{image_path.stem}_blur{image_path.suffix}"
        with Image.open(image_path) as img:
            blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
            blurred.save(output_path)
        return output_path

    # ---- 文件构建 ----

    @staticmethod
    def imgs_to_pdf(img_paths: list[Path], save_path: Path) -> Path:
        if not img_paths:
            raise ParseException("图片列表为空")
        images: list[Image.Image] = []
        for img_path in img_paths:
            with Image.open(img_path) as img:
                images.append(img.convert("RGB"))
        images[0].save(
            save_path,
            "PDF",
            save_all=True,
            append_images=images[1:],
        )
        return save_path

    @staticmethod
    def build_gif_sync(
        zip_path: Path, frames: list[dict[str, Any]], gif_path: Path
    ) -> None:
        images: list[Image.Image] = []
        durations: list[int] = []
        with zipfile.ZipFile(zip_path) as zf:
            for frame in frames:
                file_name = frame.get("file", "")
                delay = frame.get("delay", 100)
                if not file_name:
                    continue
                with zf.open(file_name) as f:
                    img = Image.open(f)
                    img.load()
                    images.append(img)
                    durations.append(delay)
        if not images:
            raise ParseException("动图帧提取失败")
        images[0].save(
            gif_path,
            "GIF",
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
        )

    @staticmethod
    def build_pdf_from_zip_sync(
        zip_path: Path, frames: list[dict[str, Any]], pdf_path: Path
    ) -> Path:
        """从动图 zip 中提取帧并合成为 PDF"""
        images: list[Image.Image] = []
        with zipfile.ZipFile(zip_path) as zf:
            for frame in frames:
                file_name = frame.get("file", "")
                if not file_name:
                    continue
                with zf.open(file_name) as f:
                    img = Image.open(f)
                    img.load()
                    images.append(img.convert("RGB"))
        if not images:
            raise ParseException("动图帧提取失败")
        images[0].save(
            pdf_path,
            "PDF",
            save_all=True,
            append_images=images[1:],
        )
        return pdf_path


class PixivParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.pixiv
        self.cookiejar = CookieJar(config, self.mycfg, domain="pixiv.net")
        self.api = PixivAPI(
            cookiejar=self.cookiejar,
            proxy=self.proxy,
            timeout=self.cfg.common_timeout,
        )

    async def close_session(self) -> None:
        await self.api.close()
        await super().close_session()


    async def _build_pdf(
        self, img_paths_task: asyncio.Task[list[Path]], pid: str
    ) -> Path:
        paths = await img_paths_task
        if not paths:
            raise ParseException("漫画图片下载失败")
        pdf_path = self.cfg.cache_dir / f"pixiv_{pid}.pdf"
        await asyncio.to_thread(PixivHelper.imgs_to_pdf, paths, pdf_path)
        return pdf_path

    async def _build_gif(self, pid: str, meta_body: dict[str, Any]) -> Path:
        zip_url = meta_body.get("originalSrc") or meta_body.get("src", "")
        if not zip_url:
            raise ParseException("动图元数据缺少 zip 地址")
        frames = meta_body.get("frames", [])
        if not frames:
            raise ParseException("动图帧数据为空")

        zip_path = await self.downloader.download_file(
            zip_url,
            file_name=f"ugoira_{pid}.zip",
            headers=PIXIV_IMG_HEADERS,
            proxy=self.proxy,
        )
        gif_path = self.cfg.cache_dir / f"pixiv_{pid}.gif"
        await asyncio.to_thread(
            PixivHelper.build_gif_sync, zip_path, frames, gif_path
        )
        return gif_path

    async def _build_ugoira_pdf(
        self, pid: str, meta_body: dict[str, Any]
    ) -> Path:
        """动图 R18：下载 zip → 提取帧 → 合成 PDF（不打码）"""
        zip_url = meta_body.get("originalSrc") or meta_body.get("src", "")
        if not zip_url:
            raise ParseException("动图元数据缺少 zip 地址")
        frames = meta_body.get("frames", [])
        if not frames:
            raise ParseException("动图帧数据为空")

        zip_path = await self.downloader.download_file(
            zip_url,
            file_name=f"ugoira_{pid}.zip",
            headers=PIXIV_IMG_HEADERS,
            proxy=self.proxy,
        )
        pdf_path = self.cfg.cache_dir / f"pixiv_{pid}.pdf"
        await asyncio.to_thread(
            PixivHelper.build_pdf_from_zip_sync, zip_path, frames, pdf_path
        )
        return pdf_path

    def _nsfw_action(self, x_restrict: int) -> str | None:
        if x_restrict <= 0:
            return None
        nsfw = self.mycfg.nsfw or "send"
        if nsfw == "ignore":
            return "ignore"
        if nsfw == "blur":
            return "blur"
        return None

    async def _get_author(self, uid: str) -> Author:
        """获取作者信息"""
        body = await self.api.get_user_detail(uid)
        name = body.get("name", "未知作者")
        avatar_url = body.get("imageBig") or body.get("image")
        return self.create_author(
            name=name,
            avatar_url=avatar_url,
            headers=PIXIV_IMG_HEADERS,
        )

    async def _download_cover(
        self, cover_url: str, blur: bool
    ) -> list[MediaContent]:
        """下载封面图，blur 时模糊处理"""
        if not cover_url:
            return []
        cover_path = await self.downloader.download_img(
            cover_url, headers=PIXIV_IMG_HEADERS, proxy=self.proxy
        )
        if blur:
            cover_path = PixivHelper.blur(cover_path, radius=5)
        return [ImageContent(cover_path)]

    async def _get_series_info(
        self, body: dict[str, Any], user_id: str
    ) -> str:
        """从 seriesNavData 提取系列进度信息"""
        series_nav = body.get("seriesNavData")
        if not series_nav or not isinstance(series_nav, dict):
            return ""

        series_id = series_nav.get("seriesId", "")
        order = series_nav.get("order", 0)
        series_title = series_nav.get("title", "")
        series_type = series_nav.get("seriesType", "manga")
        series_url = (
            f"{PIXIV_BASE}/user/{user_id}/series/{series_id}"
            if series_id and user_id
            else ""
        )
        series_label = "小说系列" if series_type == "novel" else "漫画系列"

        total: int | None = None
        if series_id:
            if series_type == "novel":
                total = await self.api.get_novel_series_total(series_id)
            else:
                total = await self.api.get_manga_series_total(
                    series_id, user_id
                )

        if total is not None:
            return (
                f"总共 {total} 话，现在下载第 {order} 话\n"
                f"{series_label}: {series_url}"
            )
        return (
            f"系列: {series_title}\n当前下载第 {order} 话\n"
            f"{series_label}: {series_url}"
        )


    @handle("pixiv.net/artworks", r"pixiv\.net/artworks/(?P<pid>\d+)")
    async def _handle_artworks(self, searched: Match[str]) -> ParseResult:
        pid = searched.group("pid")
        body = await self.api.get_illust_detail(pid)
        illust_type = int(body.get("illustType", 0))
        if illust_type == 1:
            return await self._handle_manga(pid, body)
        return await self._handle_illust(pid, body)

    @handle("pid", r"(?<![a-zA-Z])pid\s*(?P<pid>\d+)")
    @handle("pixivid", r"(?<![a-zA-Z])pixivid\s*(?P<pid>\d+)")
    async def _handle_pid(self, searched: Match[str]) -> ParseResult:
        pid = searched.group("pid")
        # 先尝试插画/漫画 API
        try:
            body = await self.api.get_illust_detail(pid)
        except ParseException:
            body = None
        if body is not None:
            illust_type = int(body.get("illustType", 0))
            if illust_type == 1:
                return await self._handle_manga(pid, body)
            return await self._handle_illust(pid, body)
        # 再尝试小说 API
        try:
            body = await self.api.get_novel_detail(pid)
        except ParseException:
            raise ParseException(f"PID {pid} 不是有效的插画/漫画/小说 ID")
        return await self._handle_novel_core(pid, body)

    @handle("pixiv.net/novel/show", r"pixiv\.net/novel/show\.php\?id=(?P<nid>\d+)")
    @handle("pixiv.net/novel/", r"pixiv\.net/novel/(?P<nid>\d+)")
    async def _handle_novel(self, searched: Match[str]) -> ParseResult:
        nid = searched.group("nid")
        body = await self.api.get_novel_detail(nid)
        return await self._handle_novel_core(nid, body)

    async def _handle_novel_core(self, nid: str, body: dict[str, Any]) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        nsfw_action = self._nsfw_action(x_restrict)
        if nsfw_action == "ignore":
            return self.result(extra={"info": "R18 内容已忽略"})

        blur = nsfw_action == "blur"
        user_id = body.get("userId", "")
        author = await self._get_author(user_id)

        title = body.get("title", "")
        text = PixivHelper.build_text(body)
        character_count = int(body.get("characterCount", 0))

        # 封面
        cover_contents = await self._download_cover(
            body.get("coverUrl", ""), blur
        )

        # 小说正文：直接从 API 的 content 字段获取
        novel_text = body.get("content", "")
        if not novel_text:
            raise ParseException("小说正文为空")
        novel_text = PixivHelper.clean_novel_text(novel_text)
        txt_path = self.cfg.cache_dir / f"pixiv_{nid}.txt"
        await asyncio.to_thread(
            lambda: txt_path.write_text(novel_text, encoding="utf-8")
        )

        send_groups: list[SendGroup] = [
            SendGroup(contents=[], render_card=True, force_merge=False),
            SendGroup(
                contents=[FileContent(txt_path, name=f"pixiv_{nid}.txt")],
                render_card=False,
                force_merge=False,
            ),
        ]

        extra: dict[str, Any] = {}
        if character_count > 0:
            extra["info"] = f"字数: {character_count}"

        return self.result(
            author=author,
            title=title,
            text=text,
            url=f"{PIXIV_BASE}/novel/show.php?id={nid}",
            contents=cover_contents,
            send_groups=send_groups,
            extra=extra,
        )

    async def _handle_illust(self, pid: str, body: dict[str, Any]) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        nsfw_action = self._nsfw_action(x_restrict)
        if nsfw_action == "ignore":
            return self.result(extra={"info": "R18 内容已忽略"})

        blur = nsfw_action == "blur"
        user_id = body.get("userId", "")
        author = await self._get_author(user_id)

        title = body.get("title", "")
        text = PixivHelper.build_text(body)
        page_count = int(body.get("pageCount", 1))
        illust_type = int(body.get("illustType", 0))
        cover_url = body.get("urls", {}).get("regular", "")
        create_date = body.get("createDate", "")

        # 封面（R18 时模糊处理）
        cover_contents = await self._download_cover(cover_url, blur)

        send_groups: list[SendGroup] = [
            SendGroup(contents=[], render_card=True, force_merge=False),
        ]

        if blur:
            # R18：封面已模糊，正文合成为 PDF
            if illust_type == 2:
                # 动图 → 提取帧 → PDF
                meta = await self.api.get_ugoira_meta(pid)
                pdf_task = asyncio.create_task(
                    self._build_ugoira_pdf(pid, meta)
                )
            else:
                # 静态插画 → 下载 → PDF
                pages = await self.api.get_pages(pid)
                img_urls = [p.get("urls", {}).get("original", "") for p in pages]
                img_urls = [u for u in img_urls if u]
                if not img_urls:
                    raise ParseException("未找到插画图片")
                img_paths_task = asyncio.create_task(
                    self.downloader.download_imgs_without_raise(
                        img_urls, headers=PIXIV_IMG_HEADERS, proxy=self.proxy
                    )
                )
                pdf_task = asyncio.create_task(
                    self._build_pdf(img_paths_task, pid)
                )
            send_groups.append(
                SendGroup(
                    contents=[FileContent(pdf_task, name=f"pixiv_{pid}.pdf")],
                    render_card=False,
                    force_merge=False,
                )
            )
        else:
            # 非 R18：正常发送图片
            content_contents: list[MediaContent] = []
            if illust_type == 2:
                # 动图 → GIF
                meta = await self.api.get_ugoira_meta(pid)
                gif_path = await self._build_gif(pid, meta)
                content_contents.append(ImageContent(gif_path))
            else:
                # 静态插画
                pages = await self.api.get_pages(pid)
                img_urls = [p.get("urls", {}).get("original", "") for p in pages]
                img_urls = [u for u in img_urls if u]
                if not img_urls:
                    raise ParseException("未找到插画图片")
                content_contents.extend(
                    self.create_image_contents(
                        img_urls, headers=PIXIV_IMG_HEADERS
                    )
                )
            send_groups.append(
                SendGroup(
                    contents=content_contents,
                    render_card=False,
                    force_merge=False,
                )
            )

        extra: dict[str, Any] = {}
        if page_count > 1:
            extra["info"] = f"共 {page_count} 张"

        return self.result(
            author=author,
            title=title,
            text=text,
            url=f"{PIXIV_BASE}/artworks/{pid}",
            contents=cover_contents,
            send_groups=send_groups,
            timestamp=PixivHelper.parse_timestamp(create_date),
            extra=extra,
        )

    async def _handle_manga(self, pid: str, body: dict[str, Any]) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        nsfw_action = self._nsfw_action(x_restrict)
        if nsfw_action == "ignore":
            return self.result(extra={"info": "R18 内容已忽略"})

        blur = nsfw_action == "blur"
        user_id = body.get("userId", "")
        author = await self._get_author(user_id)

        title = body.get("title", "")
        text = PixivHelper.build_text(body)
        page_count = int(body.get("pageCount", 1))
        cover_url = body.get("urls", {}).get("regular", "")
        create_date = body.get("createDate", "")

        # 系列信息
        extra_info = await self._get_series_info(body, user_id)

        # max_page 限制
        max_page = self.mycfg.max_page or 0
        if max_page > 0 and page_count > max_page:
            cover_contents = await self._download_cover(cover_url, blur)
            max_page_msg = (
                f"漫画共 {page_count} 页，超过最大页数 {max_page}，跳过下载。"
            )
            extra: dict[str, Any] = {}
            if extra_info:
                extra["info"] = f"{extra_info}\n{max_page_msg}"
            else:
                extra["info"] = max_page_msg
            return self.result(
                author=author,
                title=title,
                text=text,
                url=f"{PIXIV_BASE}/artworks/{pid}",
                contents=cover_contents,
                send_groups=[ # 如果只构建卡片会再发一次纯文本，不确定是不是sender的bug，先用发送封面图缓解
                    SendGroup(contents=[], render_card=True, force_merge=False),
                    SendGroup(contents=cover_contents, render_card=False, force_merge=False),
                ],
                timestamp=PixivHelper.parse_timestamp(create_date),
                extra=extra,
            )

        # 封面
        cover_contents = await self._download_cover(cover_url, blur)

        # 下载漫画页并构建 PDF
        pages = await self.api.get_pages(pid)
        img_urls = [p.get("urls", {}).get("original", "") for p in pages]
        img_urls = [u for u in img_urls if u]
        if not img_urls:
            raise ParseException("未找到漫画图片")

        img_paths_task = asyncio.create_task(
            self.downloader.download_imgs_without_raise(
                img_urls, headers=PIXIV_IMG_HEADERS, proxy=self.proxy
            )
        )
        pdf_task = asyncio.create_task(self._build_pdf(img_paths_task, pid))

        send_groups: list[SendGroup] = [
            SendGroup(contents=[], render_card=True, force_merge=False),
            SendGroup(
                contents=[FileContent(pdf_task, name=f"pixiv_{pid}.pdf")],
                render_card=False,
                force_merge=False,
            ),
        ]

        extra: dict[str, Any] = {}
        if extra_info:
            extra["info"] = extra_info

        return self.result(
            author=author,
            title=title,
            text=text,
            url=f"{PIXIV_BASE}/artworks/{pid}",
            contents=cover_contents,
            send_groups=send_groups,
            timestamp=PixivHelper.parse_timestamp(create_date),
            extra=extra,
        )
