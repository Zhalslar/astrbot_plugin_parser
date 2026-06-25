from re import Match
from typing import ClassVar
import hashlib
from curl_cffi import requests as curl_requests
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from PIL import Image, ImageFilter
from pathlib import Path

from ..config import PluginConfig
from ..data import ParseResult, Platform, VideoContent
from ..download import Downloader
from .base import BaseParser, handle
from ..exception import ParseException

_IMPERSONATE = "chrome120"
IWARA_SALT = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"

class api:

    cookie = ""
    proxy: str | None = None


    @staticmethod
    def _get_iwara_xversion(fileURL: str) -> str:
        """根据fileURL计算出xversion(要xversion才能获取原画视频链接)"""
        parsed = urlparse(fileURL)
        file_id = parsed.path.rstrip("/").split("/")[-1]  # 从URL路径中提取file_id
        params = parse_qs(parsed.query)  # fileurl的params转字典
        expires = params.get("expires", [None])[0]
        if not expires:
            raise ValueError("无法从 fileURL 中提取 expires")
        if not file_id:
            raise ValueError("无法从 fileURL 中提取 file_id")
        raw = f"{file_id}_{expires}{IWARA_SALT}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    async def iwaraID_Get_videoInfo(video_id: str) -> dict:
        """根据video_id获取视频信息"""
        url = f"https://api.iwara.tv/video/{video_id}"
        headers = {
            "accept": "application/json",
            "origin": "https://www.iwara.tv",
            "referer": "https://www.iwara.tv/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
            "cookie": api.cookie,
        }
        async with curl_requests.AsyncSession(
            timeout=10.0,
            impersonate=_IMPERSONATE,
        ) as client:
            try:
                response = await client.get(url, headers=headers, proxy=api.proxy)
                return response.json()
            except Exception as e:
                raise ParseException(f"获取iwara信息失败：{e}")
    
    @staticmethod
    async def fileURL_get_urlInfo(fileURL: str) -> dict:
        """根据fileURL获取视频链接json"""
        x_version = api._get_iwara_xversion(fileURL)
        headers = {
            "x-version": x_version
        }
        async with curl_requests.AsyncSession(
            timeout=10.0,
            impersonate=_IMPERSONATE,
        ) as client:
            try:
                response = await client.get(fileURL, headers=headers, proxy=api.proxy)
                return response.json()
            except Exception as e:
                raise ParseException(f"获取Iwara视频信息失败：{e}")
    
    @staticmethod
    async def urlInfo_Get_videoURL(urlInfo: list | dict, quality: str) -> str:
        """根据视频链接json获取指定清晰度视频链接，不存在则按优先级回退"""
        # urlInfo 可能是列表（直接返回）或带 results 的字典
        results = urlInfo if isinstance(urlInfo, list) else urlInfo.get("results", [])
        url_list = {}
        for video in results:
            url_list[video["name"]] = f"https:{video['src']['download']}"

        # 清晰度回退顺序：Source -> preview -> 540 -> 360
        fallback_order = ["Source", "preview", "540", "360"]
        # 从请求的清晰度开始，按顺序尝试
        start_idx = fallback_order.index(quality) if quality in fallback_order else -1
        if start_idx == -1:
            start_idx = 0  # 不在列表中则从最高画质开始

        for q in fallback_order[start_idx:]:
            if q in url_list:
                return url_list[q]

        raise ParseException(f"未找到可用清晰度的视频下载链接")

    @staticmethod
    def videoInfo_Get_Thumbnail(info: dict):
        """获取封面图片链接"""
        id = info.get("file", {}).get("id", "")
        thumbnail = info.get("thumbnail", "")
        url = f"https://i.iwara.tv/image/thumbnail/{id}/thumbnail-{str(thumbnail).zfill(2)}.jpg"
        return url
    
    @staticmethod
    def auto_blur_video_thumbnail(video_thumbnail: Path, rating: str, config: str) -> Path | None:
        """判断是否要增加模糊，需要提供封面、rating和设置"""
        if rating == "ecchi":
            if config == "send":
                return video_thumbnail
            else:
                output_path = video_thumbnail.parent / f"{video_thumbnail.stem}_blur{video_thumbnail.suffix}"
                return api._blur(video_thumbnail, output_path)
        else:
            return video_thumbnail


    @staticmethod
    def _blur(image_path: str | Path, output_path: str | Path | None = None, radius: int = 15) -> Path:
        """对图片施加全局高斯模糊

        Args:
            image_path: 输入图片路径
            output_path: 输出图片路径，为 None 时覆盖原图
            radius: 模糊半径
        """
        image_path = Path(image_path)
        if output_path is None:
            output_path = image_path
        else:
            output_path = Path(output_path)
        with Image.open(image_path) as img:
            blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
            blurred.save(output_path)
        return output_path
    




class IwaraParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="iwara", display_name="iwara")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.iwara
        api.cookie = self.mycfg.cookies if self.mycfg.cookies else ""
        api.proxy = self.proxy
        
    @handle("iwara.tv", r"iwara\.tv/video/(?P<video_id>\w+)")
    async def _parse(self, searched: Match[str]) -> ParseResult:
        video_id = searched.group("video_id")
        video_info = await api.iwaraID_Get_videoInfo(video_id)
        
        # 视频元数据
        video_title = video_info["title"]
        video_body = video_info["body"]
        video_tags = [tag["id"] for tag in video_info["tags"]]
        video_user = video_info["user"]["name"]
        video_user_username = video_info["user"]["username"]
        video_upload_time = video_info["updatedAt"]
        video_thumbnail = api.videoInfo_Get_Thumbnail(video_info)
        timestamp = int(datetime.fromisoformat(video_upload_time.replace("Z", "+00:00")).timestamp())
        video_duration = video_info["file"]["duration"]
        user_avatar = video_info["user"]["avatar"]
        r18 = video_info["rating"]
        video_user_avatar = (
            f"https://i.iwara.tv/image/avatar/{user_avatar['id']}/{user_avatar['name']}" # 获取用户头像
            if user_avatar
            else "https://www.iwara.tv/images/default-avatar.jpg" # iwara 默认头像
        )

        if r18 == "ecchi" and self.mycfg.nsfw == "ignore":
            return self.result(
                title=video_title,
                extra={"info": f"⚠ 该视频为 R18 内容，已按配置忽略"},
                url=f"https://www.iwara.tv/video/{video_id}",
            )

        img_path = await self.downloader.download_img(video_thumbnail, proxy=self.proxy)
        video_thumbnail_img = api.auto_blur_video_thumbnail(img_path, r18, self.mycfg.nsfw or "blur")
        
        # 获取视频下载链接
        quality = self.mycfg.video_quality if self.mycfg.video_quality else "Source"
        fileURL = video_info["fileUrl"]
        urlInfo = await api.fileURL_get_urlInfo(fileURL)
        video_url = await api.urlInfo_Get_videoURL(urlInfo, quality)

        # 构建发送信息
        send_info = f"视频描述: {video_body}\n\nTAG: {', '.join(f'#{tag}' for tag in video_tags)}"

        video_contents = VideoContent(
            path_task = self.downloader.download_video(video_url),
            cover = video_thumbnail_img if video_thumbnail_img else None,
            duration = video_duration,
            )


        author = self.create_author(
            name=f"{video_user} ({video_user_username})",
            avatar_url=video_user_avatar,
        )
        return self.result(
            title = video_title,
            text = send_info,
            author=author,
            timestamp=timestamp,
            contents=[video_contents],
            url=f"https://www.iwara.tv/video/{video_id}"
        )