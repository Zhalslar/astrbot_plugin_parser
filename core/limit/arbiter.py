import asyncio
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@dataclass(frozen=True)
class _ArbiterContext:
    """
    仲裁所需的最小不可变上下文。

    任一字段缺失或不合法，都视为不满足协议前提。
    """

    message_id: int
    msg_time: int
    self_id: int


class EmojiLikeArbiter:
    """
    基于固定表情点赞状态的弱一致分布式仲裁器。

    设计目标：
    - 在多个 Bot 同时处理同一条消息时，确定唯一赢家
    - 不依赖本地状态、不依赖外部存储、不使用随机源

    协议约束（必须遵守）：
    - raw_message.time 字段必须存在，且来源于消息原始推送
    - 时间精度为「秒级整数」，所有 Bot 观测值必须一致
    - 仅适用于“消息触发后立刻调用”的场景
    - 不支持延迟调用、历史重放或补偿
    - 所有 Bot 必须使用完全一致的协议参数

    注意：
    本类中的协议参数严禁配置化。
    任意实例参数不一致都会破坏全局一致性。
    """

    # ===== 协议常量（严禁配置化） =====

    _EMOJI_ID = 282
    _EMOJI_TYPE = "1"

    # 等待窗口需覆盖：set_like → 服务端同步 → fetch 可见
    _WAIT_SEC = 1.5

    # 将消息时间映射为确定性赢家的时间片
    _TIME_SLICE = 60

    # ===== 对外接口 =====

    async def compete(self, event: AiocqhttpMessageEvent) -> bool:
        """
        参与仲裁。

        返回：
            True  : 当前 Bot 成为赢家，应继续执行重逻辑
            False : 非赢家或不满足协议前提
        """
        ctx = self._parse_event_context(event)
        if ctx is None:
            return False

        client = event.bot
        mid = ctx.message_id

        # Phase 1：检查是否已有参与者
        users = await self._fetch_users(client, mid)
        if users:
            return False

        # Phase 2：尝试占坑
        try:
            await client.set_msg_emoji_like(
                message_id=mid,
                emoji_id=self._EMOJI_ID,
                set=True,
            )
        except Exception as e:
            # 占坑失败必须立即退出，避免不一致状态
            logger.warning(
                f"[arbiter][io] set_msg_emoji_like failed: {e}", exc_info=True
            )
            return False

        # Phase 3：等待仲裁窗口
        await asyncio.sleep(self._WAIT_SEC)

        # Phase 4：收集最终参与者
        users = await self._fetch_users(client, mid)
        if not users:
            # API 延迟兜底：允许当前实例视为成功
            return True

        # Phase 5：确定赢家
        return self._decide(users, ctx.self_id, ctx.msg_time)

    # ===== 协议前置解析 =====

    def _parse_event_context(
        self, event: AiocqhttpMessageEvent
    ) -> _ArbiterContext | None:
        """
        严格解析仲裁所需的最小上下文。

        任一字段异常即返回 None，
        表示该事件不满足仲裁协议前提。
        """
        try:
            message_id = int(event.message_obj.message_id)
            self_id = int(event.get_self_id())
        except Exception:
            logger.warning("[arbiter][protocol] invalid message_id or self_id")
            return None

        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            logger.warning(
                f"[arbiter][protocol] message({message_id}) raw_message is not dict"
            )
            return None

        msg_time = raw.get("time")
        if not isinstance(msg_time, (int, float)):  # noqa: UP038
            logger.warning(
                f"[arbiter][protocol] message({message_id}) missing valid time field"
            )
            return None

        return _ArbiterContext(
            message_id=message_id,
            msg_time=int(msg_time),
            self_id=self_id,
        )

    # ===== 内部方法 =====

    async def _fetch_users(self, bot, message_id: int) -> list[int]:
        """
        获取给指定消息贴了协议表情的用户 tinyId 集合。

        返回的是“当前观测到的参与者集合”，
        不区分 Bot 或人工用户（由协议前提保证安全）。
        """
        try:
            resp = await bot.fetch_emoji_like(
                message_id=message_id,
                emojiId=str(self._EMOJI_ID),
                emojiType=self._EMOJI_TYPE,
            )
        except Exception as e:
            logger.warning(f"[arbiter][io] fetch_emoji_like failed: {e}")
            return []

        likes = (resp or {}).get("emojiLikesList") or []
        users: list[int] = []

        for item in likes:
            try:
                users.append(int(item["tinyId"]))
            except Exception:
                continue

        return users

    def _decide(self, users: list[int], self_id: int, msg_time: int) -> bool:
        """
        基于确定性规则选择赢家。

        保证：
        - 同一条消息
        - 同一参与者集合
        - 在所有 Bot 上计算结果完全一致
        """
        try:
            participants = sorted(set(users))
            if not participants:
                raise ValueError("empty participants")

            index = (msg_time // self._TIME_SLICE) % len(participants)
            winner = participants[index]
        except Exception as e:
            logger.warning(f"[arbiter][protocol] decision failed: {e}")
            return False

        return winner == self_id
