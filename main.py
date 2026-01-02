# main.py

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from .core.debounce import LinkDebouncer
from .core.download import Downloader
from .core.exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .core.parsers import (
    BaseParser,
    BilibiliParser,
    YouTubeParser,
)
from .core.render import Renderer
from .core.utils import extract_json_url, save_cookies_with_netscape


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=2)

        # 插件数据目录
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_parser")
        config["data_dir"] = str(self.data_dir)

        # 缓存目录
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)
        self.config.save_config()

        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}

        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

        # 渲染器
        self.renderer = Renderer(config)

        # 下载器
        self.downloader = Downloader(config)

        # 防抖器
        self.debouncer = LinkDebouncer(config)

        # 仲裁器
        self.arbiter = EmojiLikeArbiter()

        # 缓存清理器
        self.cleaner = CacheCleaner(self.context, self.config)

    # region 生命周期

    async def initialize(self):
        """加载、重载插件时触发"""
        # ytb_cookies
        if self.config["ytb_ck"]:
            ytb_cookies_file = self.data_dir / "ytb_cookies.txt"
            ytb_cookies_file.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                save_cookies_with_netscape,
                self.config["ytb_ck"],
                ytb_cookies_file,
                "youtube.com",
            )
            self.config["ytb_cookies_file"] = str(ytb_cookies_file)
            self.config.save_config()
        # 加载资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器"""
        # 获取所有解析器
        all_subclass = BaseParser.get_all_subclass()
        # 过滤掉禁用的平台
        enabled_classes = [
            _cls
            for _cls in all_subclass
            if _cls.platform.display_name in self.config["enable_platforms"]
        ]
        # 启用的平台
        platform_names = []
        for _cls in enabled_classes:
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser
        logger.info(f"启用平台: {'、'.join(platform_names)}")

        # 关键词-正则对，一次性生成并排序
        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in enabled_classes
            for kw, pt in cls._key_patterns
        ]
        # 长关键词优先
        patterns.sort(key=lambda x: -len(x[0]))
        keywords = [kw for kw, _ in patterns]
        logger.debug(f"关键词-正则对已生成：{keywords}")
        self.key_pattern_list = patterns

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    def _build_send_plan(self, result: ParseResult):
        light_contents = []
        heavy_contents = []

        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            match cont:
                case ImageContent() | GraphicsContent():
                    light_contents.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy_contents.append(cont)
                case _:
                    light_contents.append(cont)

        heavy_count = len(heavy_contents)
        light_count = len(light_contents)

        base_seg_count = heavy_count + light_count

        is_single_heavy = heavy_count == 1 and light_count == 0

        render_card = is_single_heavy and self.config.get(
            "single_heavy_render_card", False
        )

        final_seg_count = base_seg_count + (1 if render_card else 0)
        force_merge = final_seg_count >= self.config["forward_threshold"]

        return {
            "light": light_contents,
            "heavy": heavy_contents,
            "render_card": render_card,
            "force_merge": force_merge,
            "preview_card": render_card and not force_merge,
        }

    async def _send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        plan = self._build_send_plan(result)

        segs: list[BaseMessageComponent] = []
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)

        # 预览卡片（单重媒体 + 不合并）
        if plan["preview_card"]:
            if image_path := await self.renderer.render_card(result):
                await event.send(event.chain_result([Image(str(image_path))]))

        # inline 卡片（合并时）
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(Image(str(image_path)))

        # 轻媒体
        for cont in plan["light"]:
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(str(path)))
                case GraphicsContent() as g:
                    segs.append(Image(str(path)))
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                if show_download_fail_tip:
                    segs.append(Plain("超过文件大小限制"))
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=str(path))
                        if self.config["audio_to_file"]
                        else Record(str(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))

        # 强制合并
        if plan["force_merge"] and segs:
            nodes = Nodes([])
            self_id = event.get_self_id()
            for seg in segs:
                nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))
            segs = [nodes]

        # 发送
        if segs:
            await event.send(event.chain_result(segs))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 禁用会话
        if umo in self.config["disabled_sessions"]:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 防抖机制：避免短时间重复处理同一链接
        link = searched.group(0)
        if self.config["debounce_interval"] and self.debouncer.hit(umo, link):
            logger.warning(f"[防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        parse_res = await self.parser_map[keyword].parse(keyword, searched)

        # 发送
        await self._send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bm")
    async def bm(self, event: AstrMessageEvent):
        """获取B站的音频"""
        text = event.message_str
        matched = re.search(r"(BV[A-Za-z0-9]{10})(\s\d{1,3})?", text)
        if not matched:
            yield event.plain_result("请发送正确的 BV 号")
            return

        bvid, page_num = matched.group(1), matched.group(2)
        page_idx = int(page_num) if page_num else 0

        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore

        _, audio_url = await parser.extract_download_urls(
            bvid=bvid, page_index=page_idx
        )
        if not audio_url:
            yield event.plain_result("未找到可下载的音频")
            return

        audio_path = await self.downloader.download_audio(
            audio_url,
            audio_name=f"{bvid}-{page_idx}.mp3",
            ext_headers=parser.headers,
            proxy=parser.proxy,
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ym")
    async def ym(self, event: AstrMessageEvent):
        """获取油管的音频"""
        text = event.message_str
        parser = self._get_parser_by_type(YouTubeParser)
        _, matched = parser.search_url(text)
        if not matched:
            yield event.plain_result("请发送正确的油管链接")
            return

        url = matched.group(0)

        audio_path = await self.downloader.download_audio(
            url, use_ytdlp=True, proxy=parser.proxy
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.check_qr_state():
            yield event.plain_result(msg)

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")
