# main.py

import asyncio
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from itertools import chain
from pathlib import Path

import aiofiles

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
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
from .core.download import Downloader
from .core.exception import DownloadException, DownloadLimitException, ZeroSizeException
from .core.limit import EmojiLikeArbiter, LinkDebouncer
from .core.parsers import (
    BaseParser,
    BilibiliParser,
    YouTubeParser,
)
from .core.render import Renderer
from .core.utils import extract_json_url, save_cookies_with_netscape


@register("astrbot_plugin_parser", "Zhalslar", "...", "...")
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

        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}

        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

        # 渲染器
        self.renderer = Renderer(config)

        # 下载器
        self.downloader = Downloader(config)

        # 仲裁器
        self.arbiter = EmojiLikeArbiter(config)

        # 防抖器
        self.debouncer = LinkDebouncer(config)

        # 会话 -> 正在运行的解析任务
        self.running_tasks: dict[str, asyncio.Task] = {}

        # 缓存清理器
        self.cleaner = CacheCleaner(self.context, self.config)


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
        self.register_parser()

    def register_parser(self):
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

    def get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    async def make_messages(self, result: ParseResult) -> list[BaseMessageComponent]:
        """组装消息"""
        segs: list[BaseMessageComponent] = []

        # 1.获取媒体内容
        failed = 0

        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            try:
                path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue  # 预期异常，不抛出
            except DownloadException:
                failed += 1
                continue

            match cont:
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))
                case VideoContent() | DynamicContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    if self.config["audio_to_file"]:
                        segs.append(File(name=path.name, file=str(path)))
                    else:
                        segs.append(Record(str(path)))
                case ImageContent():
                    segs.append(Image(str(path)))
                case GraphicsContent() as g:
                    segs.append(Image(str(path)))
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 2. 生成帖子卡片
        need_card = not self.config["simple_mode"] or not segs
        if need_card and result.render_image is None:
            cache_key = uuid.uuid4().hex
            cache_file = self.cache_dir / f"card_{cache_key}.png"
            try:
                image = await self.renderer.create_card_image(result)
                output = BytesIO()
                await asyncio.to_thread(image.save, output, format="PNG")
                async with aiofiles.open(cache_file, "wb+") as f:
                    await f.write(output.getvalue())
                result.render_image = cache_file
            except Exception:
                result.render_image = None

        # 3.插入卡片
        if result.render_image is not None:
            card_seg = Image(str(result.render_image))
            segs.insert(0, card_seg)

        # 4. 下载失败提示
        if failed:
            segs.append(Plain(f"{failed} 项媒体下载失败"))

        return segs

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

        # 防抖机制：避免短时间重复处理同一链接
        link = searched.group(0)
        if self.config["debounce_interval"] and self.debouncer.hit(umo, link):
            logger.warning(f"[防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 仲裁机制：谁贴的表情ID值最小，谁来解析
        if isinstance(event, AiocqhttpMessageEvent):
            is_win = await self.arbiter.compete(
                bot=event.bot,
                message_id=int(event.message_obj.message_id),
                self_id=int(self_id),
            )
            logger.debug(f"仲裁结果: {is_win}")
            if not is_win:
                return

        # 耗时任务：解析+渲染+合并+发送
        task = asyncio.create_task(self.job(event, keyword, searched))
        self.running_tasks[umo] = task
        try:
            await task
        except asyncio.CancelledError:
            logger.debug(f"任务被取消 - {umo}")
            return
        finally:
            self.running_tasks.pop(umo, None)

    async def job(self, event: AstrMessageEvent, keyword: str, searched: re.Match[str]):
        """一个耗时的任务包：解析+渲染+合并+发送"""
        # 解析
        parse_res = await self.parser_map[keyword].parse(keyword, searched)
        # 渲染
        segs = await self.make_messages(parse_res)
        # 合并
        if len(segs) >= self.config["forward_threshold"]:
            nodes = Nodes([])
            self_id = event.get_self_id()
            name = "解析器"
            for seg in segs:
                node = Node(uin=self_id, name=name, content=[seg])
                nodes.nodes.append(node)
            segs.clear()
            segs.append(nodes)
        # 发送
        if segs:
            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                logger.error(f"发送消息失败: {e}")

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

        parser: BilibiliParser = self.get_parser_by_type(BilibiliParser)  # type: ignore

        _, audio_url = await parser.extract_download_urls(
            bvid=bvid, page_index=page_idx
        )
        if not audio_url:
            yield event.plain_result("未找到可下载的音频")
            return

        audio_path = await self.downloader.download_audio(
            audio_url, audio_name=f"{bvid}-{page_idx}.mp3", ext_headers=parser.headers
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ym")
    async def ym(self, event: AstrMessageEvent):
        """获取油管的音频"""
        text = event.message_str
        parser = self.get_parser_by_type(YouTubeParser)
        _, matched = parser.search_url(text)
        if not matched:
            yield event.plain_result("请发送正确的油管链接")
            return

        url = matched.group(0)

        audio_path = await self.downloader.download_audio(url, use_ytdlp=True)
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self.get_parser_by_type(BilibiliParser)  # type: ignore
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

    async def terminate(self):
        """插件卸载时"""
        # 取消所有解析任务
        for task in list(self.running_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.running_tasks.values(), return_exceptions=True)
        self.running_tasks.clear()
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话
        await BaseParser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()
