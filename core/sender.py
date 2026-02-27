from itertools import chain
from pathlib import Path
from astrbot.api import logger

from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _build_send_plan(self, result: ParseResult) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            match cont:
                case ImageContent() | GraphicsContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 仅在“单一重媒体且无其他内容”时，才允许渲染卡片
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        # 实际消息段数量（卡片也算一个段）
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }


    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            await event.send(event.chain_result([Image(str(image_path))]))


    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 标题默认发送
        if result.title:
            segs.append(Plain(result.title))

        # 单个 text / 单个 textnode 场景：无媒体内容时发送正文
        if result.text and not plan["light"] and not plan["heavy"]:
            segs.append(Plain(result.text))

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(Image(str(image_path)))

        # 轻媒体处理
        for cont in plan["light"]:
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(str(path)))
                case GraphicsContent() as g:
                    segs.append(Image(str(path)))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(Plain("此项媒体超过大小限制"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("无法下载该媒体"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=str(path))
                        if self.cfg.audio_to_file
                        else Record(str(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))

        # 统计信息仅在消息层发送
        if stats_info := result.extra.get("stats"):
            segs.append(Plain(str(stats_info)))

        return segs


    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        # 当消息较多时，按 66 条一组合并，避免单个合并消息过大
        chunk_size = 66 if len(segs) > 90 else len(segs)

        logger.info(f"合并前的消息数量: {len(segs)}, 分块大小: {chunk_size}")
        
        merged_batches: list[BaseMessageComponent] = []

        for i in range(0, len(segs), chunk_size):
            chunk = segs[i : i + chunk_size]
            nodes = Nodes([])
            for seg in chunk:
                nodes.nodes.append(Node(uin="1403206256", name="樱花朝日", content=[seg]))
            merged_batches.append(nodes)

        logger.info(f"合并后的消息包的数量: {len(merged_batches)}")

        return merged_batches


    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        plan = self._build_send_plan(result)

        await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan)

        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if segs:
            if plan["force_merge"] and len(segs) > 1:
                logger.info(f"正在发送合并后的消息包，共 {len(segs)} 个消息包")
                for seg in segs:
                    await event.send(event.chain_result([seg]))
            else:
                logger.info(f"正在发送消息，共 {len(segs)} 条消息")
                await event.send(event.chain_result(segs))
