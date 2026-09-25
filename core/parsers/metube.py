import asyncio
import re
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout

from astrbot.api import logger

from ..config import PluginConfig
from ..data import VideoContent
from ..download import Downloader
from ..exception import DownloadException, SizeLimitException
from ..utils import generate_file_name, safe_unlink
from .base import BaseParser, Platform, handle


class MetubeParser(BaseParser):
    """通过 Metube 服务下载 YouTube 视频的解析器

    流程：提交链接给 Metube -> 轮询任务状态 -> 从 Metube 取回视频文件。
    链接匹配与油管解析器相同，本解析器注册顺序在其之后，
    启用后会在 parser_map 中覆盖油管解析器，接管视频链接的解析。
    """

    # 轮询 Metube 任务状态的间隔（秒）
    POLL_INTERVAL: ClassVar[float] = 3.0

    # 清晰度回退梯度（与 Metube quality 枚举一致，由高到低）
    QUALITY_LADDER: ClassVar[tuple[str, ...]] = (
        "2160",
        "1440",
        "1080",
        "720",
        "480",
        "360",
        "240",
    )

    # 从 YouTube 链接中提取视频 ID（Metube 的任务 ID 即视频 ID）
    VIDEO_ID_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z\d_-]{11})"
    )

    # 平台信息
    platform: ClassVar[Platform] = Platform(name="metube", display_name="油管(Metube)")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.metube
        self.headers.update({"Referer": "https://www.youtube.com/"})
        self._api: ClientSession | None = None

    # ---------------- Metube API ----------------

    @property
    def api_base(self) -> str:
        """Metube 服务地址（去除尾部斜杠）。API 通常位于内网，不走代理"""
        return (self.mycfg.metube_url or "http://127.0.0.1:8081").rstrip("/")

    @property
    def api(self) -> ClientSession:
        """访问 Metube API 的独立会话（无会话级超时，由管线截止时间统一约束）"""
        if self._api is None or self._api.closed:
            self._api = ClientSession(timeout=ClientTimeout(total=None))
        return self._api

    async def close_session(self) -> None:
        if self._api and not self._api.closed:
            await self._api.close()
            self._api = None
        await super().close_session()

    async def _request_json(self, method: str, path: str, **kwargs) -> dict:
        """请求 Metube API 并解析 JSON 响应（无单请求超时，调用方用管线截止时间约束）"""
        async with self.api.request(
            method, f"{self.api_base}{path}", **kwargs
        ) as resp:
            if resp.status >= 400:
                detail = (await resp.text())[:200]
                raise ClientError(f"HTTP {resp.status} {resp.reason} {detail}")
            return await resp.json(content_type=None)

    @staticmethod
    def _is_target(item: dict, submit_url: str, video_id: str | None) -> bool:
        """判断 history 条目是否为目标任务"""
        return item.get("url") == submit_url or (
            bool(video_id) and item.get("id") == video_id
        )

    @staticmethod
    def _normalize_ts(ts) -> float:
        """Metube 入队时间戳归一化为秒级 epoch（兼容 ns/ms/s 精度）"""
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return 0.0
        if ts > 1e17:  # 纳秒
            ts /= 1e9
        elif ts > 1e11:  # 毫秒
            ts /= 1e3
        return ts

    async def _find_history_entry(
        self, submit_url: str, video_id: str | None
    ) -> dict | None:
        """在 /history 中查找本视频的最新任务条目，找不到返回 None"""
        history = await self._request_json("GET", "/history")
        candidates = [
            item
            for group in ("done", "queue", "pending")
            for item in history.get(group) or []
            if self._is_target(item, submit_url, video_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: i.get("timestamp") or 0)

    @staticmethod
    def _cache_name(video_id: str | None) -> str | None:
        """缓存文件名：仅用视频 ID，命中即跳过整条管线与元数据提取"""
        return f"{video_id}.mp4" if video_id else None

    async def _wait_finished(
        self,
        submit_url: str,
        video_id: str | None,
        add_task: asyncio.Task,
        deadline: float,
    ) -> dict:
        """轮询 /history 直到任务真正完成（受管线截止时间约束，无单请求超时）

        Metube 的 /add 会同步解析链接后才入队并响应 {'status': 'ok'}，
        解析期间条目尚未出现，因此不能依赖 /add 的响应时序：
        - /add 响应 error → 快速失败；连接异常 → 服务异常
        - done 队列按 URL 键存储且可能残留同 URL 的历史条目，
          仅采信入队时间晚于本次提交时刻（含时钟容差）的条目
        - status=finished 会为每个下载流各触发一次（如 .f133.mp4 纯视频流），
          条目从 queue 移入 done 才代表合并/后处理全部结束
        """
        timeout = self.mycfg.wait_timeout or 600
        submit_epoch = time.time()
        add_ok = False
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DownloadException(f"等待 Metube 下载超时({timeout}秒)")

            # /add 已完成时优先处理其结果
            if add_task.done():
                exc = add_task.exception()
                if exc is not None:
                    raise DownloadException(f"Metube 服务异常: {exc}") from exc
                resp = add_task.result()
                if resp.get("status") != "ok":
                    logger.warning(
                        f"[metube] 添加任务被拒绝: "
                        f"{resp.get('msg') or resp} | {submit_url}"
                    )
                    raise DownloadException(
                        f"Metube 添加任务失败: {resp.get('msg') or resp}"
                    )
                add_ok = True

            try:
                history = await asyncio.wait_for(
                    self._request_json("GET", "/history"), remaining
                )
            except TimeoutError:
                raise DownloadException(
                    f"等待 Metube 下载超时({timeout}秒)"
                ) from None

            live = next(
                (
                    item
                    for group in ("queue", "pending")
                    for item in history.get(group) or []
                    if self._is_target(item, submit_url, video_id)
                ),
                None,
            )
            if live is not None:
                if live.get("status") == "error":
                    raise DownloadException(
                        f"Metube 下载失败: "
                        f"{live.get('msg') or live.get('error') or '未知错误'}"
                    )
            else:
                entry = next(
                    (
                        item
                        for item in history.get("done") or []
                        if self._is_target(item, submit_url, video_id)
                    ),
                    None,
                )
                # 仅采信本次提交之后入队的 done 条目，历史残留继续等待
                fresh = entry is not None and self._normalize_ts(
                    entry.get("timestamp")
                ) >= submit_epoch - 60
                if entry is not None and fresh:
                    if entry.get("status") == "error":
                        raise DownloadException(
                            f"Metube 下载失败: "
                            f"{entry.get('msg') or entry.get('error') or '未知错误'}"
                        )
                    if entry.get("filename"):
                        return entry
                elif add_ok:
                    # 已确认入队却无在队/新鲜条目 → 任务被删除
                    raise DownloadException("Metube 任务不存在或已被删除")
                # 其余情况: Metube 仍在解析或仅有历史残留 → 继续等待
            if asyncio.get_running_loop().time() >= deadline:
                raise DownloadException(f"等待 Metube 下载超时({timeout}秒)")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _delete_download(self, download_id: str, where: str) -> None:
        """删除 Metube 下载记录（尽力而为；清理动作 15 秒封顶防止挂起）"""
        try:
            await asyncio.wait_for(
                self._request_json(
                    "POST", "/delete", json={"ids": [download_id], "where": where}
                ),
                15,
            )
        except (ClientError, TimeoutError) as e:
            logger.warning(f"[metube] 删除记录 {download_id} 失败: {e}")

    async def _download_via_metube(
        self, url: str, quality: str | None = None, cache_name: str | None = None
    ) -> Path:
        """提交链接给 Metube，等待完成后把视频取回到缓存目录

        整条管线（提交→解析等待→下载→取回）共用 wait_timeout 一个截止时间，
        内部请求不设单独超时
        """
        timeout = self.mycfg.wait_timeout or 600
        deadline = asyncio.get_running_loop().time() + timeout
        submit_url = self._strip_timestamp_param(url)
        payload = {
            "url": submit_url,
            "download_type": "video",
            "quality": quality or self.mycfg.video_quality or "720",
            "codec": self.mycfg.video_codec or "h264",
            "format": self.mycfg.video_format or "mp4",
            "auto_start": True,
        }
        # /add 会同步解析链接后才响应，慢代理下可能长时间无响应。
        # 因此把请求放入后台任务、不阻塞等待响应，以 /history 中任务条目
        # 的出现为准；/add 的 error 状态仅用于快速失败
        add_task = asyncio.create_task(
            self._request_json("POST", "/add", json=payload)
        )
        video_id = (
            match.group(1) if (match := self.VIDEO_ID_RE.search(submit_url)) else None
        )
        logger.info(
            f"[metube] 已提交下载任务(等待 Metube 解析): {video_id or submit_url} | {url}"
        )

        try:
            finished = await self._wait_finished(
                submit_url, video_id, add_task, deadline
            )
            # 第二层限制：Metube 已完成下载，实际体积超限则不取回，直接清理
            size = finished.get("size")
            if isinstance(size, (int, float)) and size > self.cfg.max_size:
                if finished.get("url"):
                    await self._delete_download(finished["url"], "done")
                raise SizeLimitException
            filename = finished.get("filename")
            if not filename:
                raise DownloadException("Metube 未返回下载文件名")
            # Metube 完成文件的静态端点
            file_url = f"{self.api_base}/download/{quote(str(filename))}"
            file_name = cache_name or generate_file_name(file_url, ".mp4")
            video_path = await self._fetch_file(file_url, file_name, deadline)
        except (DownloadException, ClientError, TimeoutError) as e:
            logger.warning(f"[metube] 下载流程失败: {e} | {url}")
            # 尽力取消 Metube 侧任务，避免孤儿下载：
            # - 条目已在队列/下载中 → 按条目 URL 取消（可终止下载进程）
            # - 仍在解析（条目未入队） → 按规范化 URL 预取消，
            #   Metube 解析完成后会检查 _canceled_urls 拒绝入队
            # done/queue 队列以规范化 URL 为键，必须用 URL，不能用视频ID
            try:
                entry = await asyncio.wait_for(
                    self._find_history_entry(submit_url, video_id), 15
                )
            except (ClientError, TimeoutError):
                entry = None
            cancelled: set[str] = set()
            if entry and entry.get("url"):
                await self._delete_download(entry["url"], "queue")
                cancelled.add(entry["url"])
            # 条目尚未入队时，分别按提交 URL 与服务端规范化 URL 预取消，
            # 覆盖 youtu.be/shorts 等提交形式与 webpage_url 规范形不一致的情况
            if submit_url not in cancelled:
                await self._delete_download(submit_url, "queue")
                cancelled.add(submit_url)
            if video_id:
                canonical = f"https://www.youtube.com/watch?v={video_id}"
                if canonical not in cancelled:
                    await self._delete_download(canonical, "queue")
            if isinstance(e, DownloadException):
                raise
            raise DownloadException(f"Metube 服务异常: {e}") from e
        finally:
            # 管线结束（含失败）时回收 /add 请求：未完成则取消，异常则吞掉
            if add_task.done():
                add_task.exception()
            else:
                add_task.cancel()
        # 拉取成功后按需清理记录，DELETE_FILE_ON_TRASHCAN=true 时会同时删除服务端文件
        if self.mycfg.delete_after_fetch and finished.get("url"):
            await self._delete_download(finished["url"], "done")
        return video_path

    async def _fetch_file(
        self, file_url: str, file_name: str, deadline: float
    ) -> Path:
        """从 Metube 静态端点取回完成文件到缓存目录（受管线截止时间约束）"""
        file_path = self.cfg.cache_dir / file_name
        if file_path.exists():
            return file_path

        async def _do() -> None:
            async with self.api.get(file_url) as resp:
                if resp.status >= 400:
                    raise DownloadException(
                        f"取回失败: HTTP {resp.status} {resp.reason}"
                    )
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)

        remaining = deadline - asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(_do(), remaining)
        except TimeoutError:
            await safe_unlink(file_path)
            raise DownloadException(
                f"等待 Metube 下载超时({self.mycfg.wait_timeout or 600}秒)"
            ) from None
        except ClientError as e:
            await safe_unlink(file_path)
            raise DownloadException(f"从 Metube 取回视频失败: {e}") from e
        except OSError as e:
            # 缓存目录不可写/磁盘满等本地文件系统错误：
            # 清理残缺文件并转为 DownloadException，让外层继续执行 Metube 取消清理
            await safe_unlink(file_path)
            raise DownloadException(f"写入缓存文件失败: {e}") from e
        return file_path

    @staticmethod
    def _strip_timestamp_param(url: str) -> str:
        """去掉 t= 时间戳参数，避免 Metube 将其解析为裁剪起点"""
        try:
            parts = urlsplit(url)
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            if not any(k == "t" for k, _ in pairs):
                return url
            query = urlencode([(k, v) for k, v in pairs if k != "t"])
            return urlunsplit(parts._replace(query=query))
        except ValueError:
            return url

    # ---------------- 解析 ----------------

    @handle("youtu", r"youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+")
    @handle(
        "youtube",
        r"youtube\.com/(?:watch|shorts)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+)",
    )
    async def _parse_video(self, searched: re.Match[str]):
        return await self.parse_video(searched)

    async def parse_video(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL；两个匹配模式均从域名开始，需补全协议
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        video_id = (
            match.group(1) if (match := self.VIDEO_ID_RE.search(url)) else None
        )
        cache_name = self._cache_name(video_id)

        # 缓存判断：仅比对视频 ID，命中即直接发送本地缓存
        # （不提取元数据、不提交 Metube）
        if cache_name:
            cache_path = self.cfg.cache_dir / cache_name
            if cache_path.exists():
                logger.info(f"[metube] 命中缓存: {cache_name}")
                return self.result(contents=[VideoContent(cache_path)])

        # 第一层限制：本地提取元数据，时长超限直接拒绝；
        # 体积按清晰度上限逐级回退，选出不撞墙的最高档
        quality: str | None = None
        duration = 0.0
        raw = await self._extract_video_meta(url)
        if raw is not None:
            duration = float(raw.get("duration") or 0)
            if duration > self.cfg.max_duration:
                reason = f"时长 {duration / 60:.1f} 分钟超限"
                logger.info(f"[metube] 视频超限, 跳过 Metube 下载 | {reason} | {url}")
                return self._reject_result(raw, reason)

            ladder = self._quality_ladder()
            chosen = None
            chosen_est = None
            for rung in ladder:
                est = self._estimate_size(raw, duration, rung)
                # 无法估算的档位按可选处理，由第二层实际大小兜底
                if est is None or est <= self.cfg.max_size:
                    chosen, chosen_est = rung, est
                    break
            if chosen is None:
                floor_est = self._estimate_size(raw, duration, ladder[-1])
                reason = (
                    f"预估体积 {floor_est / 1048576:.1f}MB 超限"
                    if floor_est
                    else "预估体积超限"
                )
                logger.info(f"[metube] 视频超限, 跳过 Metube 下载 | {reason} | {url}")
                return self._reject_result(raw, reason)
            est_mb = (
                f"{chosen_est / 1048576:.1f}MB" if chosen_est is not None else "未知"
            )
            logger.info(f"[metube] 清晰度选择: {chosen} (预估 {est_mb}) | {url}")
            quality = chosen

        # 提交 Metube 并在后台等待下载完成（元信息缺失时完全交由 Metube 处理）
        video_task = asyncio.create_task(
            self._download_via_metube(url, quality=quality, cache_name=cache_name),
            name=f"metube | {url}",
        )
        contents = [
            self.create_video_content_by_task(
                video_task,
                duration=duration,
            )
        ]
        return self.result(
            title=raw.get("title") if raw else None,
            author=self.create_author(raw["channel"])
            if raw and raw.get("channel")
            else None,
            contents=contents,
            timestamp=raw.get("timestamp") if raw else None,
        )

    def _reject_result(self, raw: dict, reason: str):
        """超限视频返回纯文本说明，不提交 Metube"""
        return self.result(
            title=raw.get("title"),
            author=self.create_author(raw["channel"]) if raw.get("channel") else None,
            text=f"视频{reason}, 已跳过下载",
        )

    async def _extract_video_meta(self, url: str) -> dict | None:
        """本地 yt-dlp 提取视频信息（主动启用 Node.js 运行时解签名）

        失败不阻断 Metube 流程
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "http_headers": self.headers,
            # YouTube 签名解算需要 JS 运行时，主动指定 Node.js
            "js_runtimes": {"node": {}},
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
        except Exception as e:
            logger.warning(f"[metube] 本地提取视频信息失败, 将由 Metube 全权处理: {e}")
            return None
        return raw if isinstance(raw, dict) else None

    def _quality_ladder(self) -> list[str]:
        """从配置的清晰度上限向下构造回退梯度"""
        top = str(self.mycfg.video_quality or "720")
        if top == "worst":
            # worst 语义为最低画质：直接取 Metube 最低枚举档，不再向上回退
            # （Metube 自身对 worst 不加分辨率限制，等价于 best，需单独处理）
            return ["240"]
        if top == "best":
            # best 不加分辨率限制，先试不限档再逐级降
            return ["best", *self.QUALITY_LADDER]
        cap = int(top)
        return [q for q in self.QUALITY_LADDER if int(q) <= cap]

    def _estimate_size(
        self, raw: dict, duration: float, quality: str | None = None
    ) -> int | None:
        """按 Metube 选择器口径估算指定清晰度下的体积（字节），无法估算返回 None

        Metube 视频选择器: bestvideo[height<=Q]{codec}{ext}+bestaudio{ext}/best{ext}
        体积来源优先级: filesize > filesize_approx > tbr*时长
        """
        quality = quality or str(self.mycfg.video_quality or "720")
        cap = int(quality) if quality.isdigit() else None
        want_mp4 = str(self.mycfg.video_format or "mp4") == "mp4"
        codec_re = {
            "h264": r"^(h264|avc)",
            "h265": r"^(h265|hevc)",
            "av1": r"^av0?1",
            "vp9": r"^vp0?9",
        }.get(str(self.mycfg.video_codec or "h264"))

        def size_of(fmt: dict) -> int | None:
            for key in ("filesize", "filesize_approx"):
                size = fmt.get(key)
                if isinstance(size, (int, float)) and size > 0:
                    return int(size)
            tbr = fmt.get("tbr")
            if isinstance(tbr, (int, float)) and tbr > 0 and duration > 0:
                return int(tbr * 1000 / 8 * duration)
            return None

        def height_ok(fmt: dict) -> bool:
            height = fmt.get("height")
            return cap is None or (
                isinstance(height, (int, float)) and height <= cap
            )

        video_cands: list[tuple[int, bool]] = []  # (体积, 是否符合编码偏好)
        audios: list[int] = []
        progressive: list[int] = []
        for fmt in raw.get("formats") or []:
            if not isinstance(fmt, dict):
                continue
            vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
            is_video, is_audio = (
                vcodec not in (None, "none"),
                acodec not in (None, "none"),
            )
            # [height<=Q] 仅约束含视频流的格式；bestaudio 无分辨率过滤
            if is_video and not height_ok(fmt):
                continue
            size = size_of(fmt)
            if size is None:
                continue
            if is_video and is_audio:
                # 渐进式格式（选择器最后的 best 兜底分支）
                if not want_mp4 or fmt.get("ext") == "mp4":
                    progressive.append(size)
            elif is_video:
                if not want_mp4 or fmt.get("ext") == "mp4":
                    video_cands.append(
                        (
                            size,
                            bool(codec_re and re.search(codec_re, str(vcodec))),
                        )
                    )
            elif is_audio:
                if not want_mp4 or fmt.get("ext") == "m4a":
                    audios.append(size)

        if video_cands:
            # 优先符合编码偏好的流，无匹配时 Metube 会回退到不限编码的分支
            prefer = [s for s, ok in video_cands if ok]
            best_video = max(prefer) if prefer else max(s for s, _ in video_cands)
            # 音频体积未知时仅按视频流估算（偏小，由第二层实际大小兜底）
            return best_video + (max(audios) if audios else 0)
        return max(progressive) if progressive else None
