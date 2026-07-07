from __future__ import annotations

from re import Match
from typing import TYPE_CHECKING, ClassVar
import asyncio
import subprocess
import sys
from PIL import Image, ImageFilter
from pathlib import Path

from astrbot.api import logger

if TYPE_CHECKING:
    from jmcomic import JmPhotoDetail

from ..config import PluginConfig
from ..data import ParseResult, Platform, ImageContent, FileContent, SendGroup, TextContent, MediaContent
from ..download import Downloader
from .base import BaseParser, handle
from ..exception import ParseException


def ensure_jmcomic_lib() -> bool:
    """
    安装/导入jmcomic库
    """
    try:
        import jmcomic
        logger.info("[parserplugin-jmcomic] jmcomic 库依赖加载完毕")
        return True
    # 导入失败
    except ImportError:
        logger.warning("[parserplugin-jmcomic] 依赖缺失，开始尝试自动安装")

    # 导入失败开始安装
    mirrors = [
        ("清华源", "https://pypi.tuna.tsinghua.edu.cn/simple"),
        ("官方 PyPI", "https://pypi.org/simple"),
    ]
    for name, index_url in mirrors:
        try:
            logger.info(f"[parserplugin-jmcomic] 正在从 {name} 安装相关依赖 ...")
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "jmcomic",
                    "-i",
                    index_url,
                ]
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"[parserplugin-jmcomic] 从 {name} 安装相关依赖失败: {e}")
            continue

        # 安装后再次尝试导入
        try:
            import jmcomic
            logger.info(f"[parserplugin-jmcomic] jmcomic 库依赖加载完毕")
            return True
        except ImportError as e:
            logger.warning(f"[parserplugin-jmcomic] 从 {name} 安装后仍导入失败: {e}，请尝试手动安装以下依赖库：jmcomic")

    logger.error("[parserplugin-jmcomic] 所有镜像源安装均失败")
    return False


class JMComicParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="jmcomic", display_name="jmcomic")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        # 启用时检测依赖
        if not ensure_jmcomic_lib():
            logger.warning("[parserplugin-jmcomic] 依赖不可用，已从配置禁用 jmcomic 解析器并保存")
            try:
                config.parser.jmcomic.enable = False
                config.save_config()
            except Exception as e:
                logger.error(f"[parserplugin-jmcomic] 禁用 jmcomic 配置失败: {e}。请手动删除 jmcomic 解析器")
            # 解关联关键词匹配
            JMComicParser._key_patterns = []
            return
        self.mycfg = config.parser.jmcomic

    @staticmethod
    def _blur(image_path: str | Path, output_path: str | Path | None = None, radius: int = 20) -> Path:
        """对图片施加全局高斯模糊

        Args:
            image_path: 输入图片路径
            output_path: 输出图片路径，为 None 时增加_blur后缀
            radius: 模糊半径
        """
        image_path = Path(image_path)
        if output_path is None:
            output_path = image_path.parent / f"{image_path.stem}_blur{image_path.suffix}"
        else:
            output_path = Path(output_path)
        with Image.open(image_path) as img:
            blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
            blurred.save(output_path)
        return output_path

    def decode_img(self, img_url: str, scramble_id: str, image_path: Path) -> Path:
        """解密禁漫下载下来的错位图片"""
        from jmcomic import JmImageTool

        split_num = JmImageTool.get_num_by_url(scramble_id, img_url)
        save_path = image_path.parent / f"{image_path.stem}_decode{image_path.suffix}"
        with Image.open(image_path) as img_src:
            JmImageTool.decode_and_save(split_num, img_src, str(save_path))  # type: ignore
        return save_path

    def imgs2PDF(self, img_paths: list[Path], save_path: Path) -> Path:
        """工具: 将本地图片列表合并为PDF文件"""
        if not img_paths:
            raise ParseException("图片列表为空，可能需要检查 jm 号是否正确")
        images = []
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

    async def _download_all_photo(self, photo_detail: JmPhotoDetail) -> list[Path]:
        """后台并发下载所有图片，返回下载并解密后的图片路径列表"""
        photos = list(photo_detail)
        urls = [p.img_url for p in photos]

        downloaded_paths = await asyncio.gather(*(
            self.downloader.download_img(u, proxy=self.proxy) for u in urls
        ))

        return [
            self.decode_img(p.img_url, p.scramble_id, path) # 解密图片
            for p, path in zip(photos, downloaded_paths)
            if path is not None
        ]
    
    async def _build_pdf(self, img_paths: asyncio.Task[list[Path]], pdf_name: str) -> Path:
        """根据图片列表构建 PDF，返回 pdf 路径"""
        paths = await img_paths
        pdf_path = self.imgs2PDF(paths, self.cfg.cache_dir / f"{pdf_name}.pdf")
        return pdf_path
    
    @handle("18comic.vip/photo", r"18comic.vip/photo/(?P<comic_id>\d{5,})")
    @handle("18comic.vip/album", r"18comic.vip/album/(?P<comic_id>\d{5,})")
    @handle("jm", r"jm(?P<comic_id>\d+)")
    async def _parse(self, searched: Match[str]) -> ParseResult:
        """解析漫画"""
        from jmcomic import (
            JmOption,
            MissingAlbumPhotoException,
            JmcomicException,
            JmcomicText,
        )

        comic_id = searched.group("comic_id").lstrip("0")
        if not comic_id:
            return self.result(extra={"info": "JM 号不能为空"})
        
        # 获取漫画详情信息
        async with JmOption.default().new_jm_async_client() as JMClient:
            # 请求详情
            try:
                album_detail = await JMClient.get_album_detail(comic_id)
            except MissingAlbumPhotoException as e:
                return self.result(extra = {"info": f"当前 JM 号不存在"})   
            except JmcomicException as e:
                raise ParseException(f"获取 jm 详情遇到问题: {e}")    
            
            
            album_title = album_detail.title
            album_oname = album_detail.authoroname
            album_actors = album_detail.actors
            album_desc = album_detail.description
            album_tags = album_detail.tags
            album_views = album_detail.views
            album_likes = album_detail.likes
            album_cover_url = JmcomicText.get_album_cover_url(comic_id)
            
            # 章节信息
            photo_detail = await JMClient.get_photo_detail(comic_id)
            episode_count = len(album_detail.episode_list)
            album_url = JmcomicText.format_album_url(comic_id)
            album_episode = photo_detail.sort

            send_groups: list[SendGroup] = []
            
            # 封面处理
            cover_contents: list[MediaContent] = []
            if self.mycfg.nsfw != "ignore": # 不忽略
                IMG_PATH = await self.downloader.download_img(album_cover_url, proxy=self.proxy)
                if IMG_PATH is None:
                    logger.warning(f"[parserplugin-jmcomic] 封面下载失败，跳过封面处理: {album_cover_url}")
                else:
                    if self.mycfg.nsfw == "blur": # 模糊后发送
                        IMG_PATH = self._blur(IMG_PATH, radius=5)
                    cover_contents = [ImageContent(IMG_PATH)]

            # 详情文字
            send_text = (
                f"标题: {album_title}\n"
                f"TAGS: {', '.join(f'#{t}' for t in album_tags)}\n"
                f"浏览: {album_views}\n"
                f"点赞: {album_likes}\n"
                f"描述: {album_desc}\n"
            )
            extra_info = f"共 {episode_count} 话，当前下载第 {album_episode} 话\n漫画原链: {album_url}"
            if episode_count > 1:
                extra_info += "\n若需解析其他章节请发送对应章节jm号。"
            
            # 预览卡片 
            send_groups.append(SendGroup( # 仅构造卡片
                contents=[],
                render_card = True,
                force_merge=False,
            ))

            # 发送处理
            send_mode = self.mycfg.image_send_mode or "pdf"
            if send_mode != "ignore":
                urls = [p.img_url for p in list(photo_detail)]

                if self.mycfg.max_page and len(urls) > self.mycfg.max_page:
                    extra_info += f"\n当前漫画页数 {len(urls)}，超过最大页数 {self.mycfg.max_page}，跳过解析。"
                
                else:
                    img_paths = asyncio.create_task(self._download_all_photo(photo_detail))
                    # PDF模式
                    if send_mode == "pdf": 
                        pdf_task = asyncio.create_task(self._build_pdf(img_paths, comic_id))
                        send_groups.append(SendGroup( # 构造pdf
                            contents=[FileContent(pdf_task)],
                            render_card = False,
                            force_merge = False,
                        ))
                    # 合并转发
                    elif send_mode == "merge":
                        img_contents: list[MediaContent] = []
                        for img in await img_paths:
                            img_contents.append(ImageContent(img))
                        send_groups.append(SendGroup( # 构造图片
                            contents=img_contents,
                            render_card = False,
                            force_merge = True,
                        ))
                

            return self.result(
                title = album_oname,
                author = self.create_author(", ".join(album_actors)),
                contents = cover_contents,
                text = send_text,
                extra = {"info": extra_info},
                send_groups=send_groups,
            )
        