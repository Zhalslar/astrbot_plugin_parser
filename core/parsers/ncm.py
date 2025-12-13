from re import Match
from typing import ClassVar

from aiohttp import ClientError

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..download import Downloader
from .base import BaseParser, handle
from .data import Platform


class NCMParser(BaseParser):
    """网易云音乐解析器"""

    platform: ClassVar[Platform] = Platform(
        name="ncm", display_name="网易云"
    )

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)


    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_url = f"https://163cn.tv/{searched.group('short_key')}"
        # 让框架跟随 302 后再走通用解析
        return await self.parse_with_redirect(short_url)


    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com/#/song\?.*id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        print(song_id)
        detail_url = (
            f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        )
        play_url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=320000"

        # 1. 取歌曲元数据
        async with self.client.get(detail_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"[NCM] 获取歌曲信息失败 HTTP {resp.status}")
            detail_json = await resp.json()
            print(f"歌曲信息: {detail_json}")

        song = detail_json.get("songs", [{}])[0]
        if not song:
            raise ValueError("[NCM] 未找到该歌曲")

        title = song.get("name", "")
        sub_title = song.get("alias", [""])[0]  # 别名
        album_name = song.get("album", {}).get("name", "")
        cover_url = song.get("album", {}).get("picUrl", "") + "?param=640y640"
        duration_ms = song.get("duration", 0)

        # 作者信息
        ar_list = song.get("artists", [])
        author_name = " / ".join(ar.get("name", "") for ar in ar_list)
        author_avatar = ar_list[0].get("img1v1Url", "") if ar_list else ""

        # 2. 取播放地址
        async with self.client.get(play_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"[NCM] 获取播放地址失败 HTTP {resp.status}")
            play_json = await resp.json()
        play_info = play_json.get("data", [{}])[0]
        audio_url = play_info.get("url", "")

        # 3. 组装结果
        author = self.create_author(author_name, author_avatar)
        audio = self.create_video_content(
            audio_url, cover_url, duration=duration_ms // 1000
        )

        # 4. 返回
        return self.result(
            title=f"{title}{'（' + sub_title + '）' if sub_title else ''}",
            text=f"专辑：{album_name}",
            author=author,
            contents=[audio],
            timestamp=None,
            url=f"https://music.163.com/#/song?id={song_id}",
        )

    # 3. 直链 mp3 —— 直接下载
    @handle("music.126.net",r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)  # 整条 url
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        # 整条原始 URL 就是直链
        private_url = searched.group(0)
        print(private_url)
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )
