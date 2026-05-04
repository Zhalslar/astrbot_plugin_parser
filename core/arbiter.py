"""
EmojiLikeArbiter 协议实现（生产级）

本实现是 EmojiLikeArbiter 协议的参考实现：
- 无状态
- 弱一致
- 确定性递补
- CQHTTP（OneBot v11）语义级通用

协议一致性仅依赖：
- 同一条消息
- 同一参与者集合
- 同一 msg_time
- 同一排序规则
- 同一固定时间窗口

⚠️ 本文件【不依赖任何机器人框架】
⚠️ 仅假设 bot 对象支持 CQHTTP 标准 action
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

# ======================================================================
# 仲裁最小不可变上下文
# ======================================================================


@dataclass(frozen=True)
class ArbiterContext:
    """
    仲裁所需的最小不可变上下文。

    任一字段缺失或非法，均视为不满足协议前提。
    """

    message_id: int
    msg_time: int
    self_id: int


# ======================================================================
# EmojiLikeArbiter 协议核心实现
# ======================================================================


class EmojiLikeArbiter:
    """
    基于 CQHTTP 表情点赞状态的弱一致分布式仲裁器（支持确定性递补）。

    协议特性：
    - 仲裁顺序一次性确定
    - 递补不重新仲裁，仅推进顺序指针
    - 表情 124 作为“胜出权存在性证明”
    """

    # ================= 协议常量（严禁配置化） =================

    _EMOJI_ID = 289
    _EMOJI_TYPE = "1"
    _WAIT_SEC = 1.0

    _FEEDBACK_EMOJI_ID = 124
    _FEEDBACK_EMOJI_TYPE = "1"
    _FEEDBACK_WAIT_SEC = 0.7

    _TIME_SLICE = 60

    # ================= 对外唯一入口 =================

    async def compete(self, bot: Any, ctx: ArbiterContext) -> bool:
        """
        执行一次完整的 EmojiLikeArbiter 仲裁流程。

        :param bot: 任意 CQHTTP Bot（支持 set_msg_emoji_like / fetch_emoji_like）
        :param ctx: 仲裁上下文（由框架侧构造）
        :return: 当前 Bot 是否为实际胜出者
        """

        mid = ctx.message_id

        # Phase 1：初始窗口检测
        if await self._fetch_users(bot, mid, self._EMOJI_ID, self._EMOJI_TYPE):
            return False

        # Phase 2：占坑
        try:
            await bot.set_msg_emoji_like(
                message_id=mid,
                emoji_id=self._EMOJI_ID,
                emoji_type=self._EMOJI_TYPE,
                set=True,
            )
        except Exception:
            return False

        # Phase 3：仲裁窗口等待
        await asyncio.sleep(self._WAIT_SEC)

        # Phase 4：参与者收集
        users = await self._fetch_users(bot, mid, self._EMOJI_ID, self._EMOJI_TYPE)
        if not users:
            # 极端 API 延迟兜底：视为成功
            return True

        # Phase 5：胜出顺序计算（仅一次）
        order = self._decide_order(users, ctx.msg_time)
        if not order:
            return False

        # Fast-Path：单参与者
        if len(order) == 1:
            return order[0] == ctx.self_id

        # Phase 6：确定性递补确认
        for candidate in order:
            if candidate == ctx.self_id:
                try:
                    await bot.set_msg_emoji_like(
                        message_id=mid,
                        emoji_id=self._FEEDBACK_EMOJI_ID,
                        emoji_type=self._FEEDBACK_EMOJI_TYPE,
                        set=True,
                    )
                except Exception:
                    pass

            await asyncio.sleep(self._FEEDBACK_WAIT_SEC)

            if await self._has_feedback(bot, mid):
                return candidate == ctx.self_id

        return False

    # ================= 内部方法 =================

    async def _fetch_users(
        self,
        bot: Any,
        message_id: int,
        emoji_id: int,
        emoji_type: str,
    ) -> list[int]:
        """
        拉取指定表情的点赞用户列表。
        """
        try:
            resp = await bot.fetch_emoji_like(
                message_id=message_id,
                emoji_id=str(emoji_id),
                emojiId=str(emoji_id),
                emojiType=emoji_type,
                count=20,
            )
        except Exception:
            return []

        likes = (resp or {}).get("emojiLikesList") or []
        users: list[int] = []

        for item in likes:
            try:
                users.append(int(item["tinyId"]))
            except Exception:
                continue

        return users

    async def _has_feedback(self, bot: Any, message_id: int) -> bool:
        """
        判断是否观测到胜出确认信号（表情 124）。
        """
        users = await self._fetch_users(
            bot,
            message_id,
            self._FEEDBACK_EMOJI_ID,
            self._FEEDBACK_EMOJI_TYPE,
        )
        return bool(users)

    def _decide_order(self, users: list[int], msg_time: int) -> list[int]:
        """
        基于确定性规则生成胜出递补顺序。

        保证：
        - 顺序在所有 Bot 上完全一致
        - 不随时间推进而变化
        """
        participants = sorted(set(users))
        if not participants:
            return []

        base = (msg_time // self._TIME_SLICE) % len(participants)
        return [
            participants[(base + i) % len(participants)]
            for i in range(len(participants))
        ]
