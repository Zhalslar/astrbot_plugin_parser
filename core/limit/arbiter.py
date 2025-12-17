"""
EmojiLikeArbiter 协议规范（生产级 · 形式化定义）

本文件定义了一种【无状态、弱一致、确定性递补】的分布式仲裁协议，
用于在多个 Bot 同时处理同一条消息时，确定唯一的“胜出执行者”。

──────────────────────────────────────────────────────────────────────
一、设计目标
──────────────────────────────────────────────────────────────────────

1. 在完全去中心化、无共享状态的前提下，确保所有 Bot 对同一消息：
   - 计算出完全一致的胜出顺序
   - 对“谁有资格继续执行重逻辑”达成一致

2. 不依赖：
   - 本地缓存 / 内存状态
   - 外部存储（数据库 / Redis）
   - 随机数
   - 额外的协调服务

3. 允许：
   - Bot 崩溃或超时
   - API 延迟或失败
   - 部分参与者不遵守协议

──────────────────────────────────────────────────────────────────────
二、协议适用范围（强约束）
──────────────────────────────────────────────────────────────────────

本协议【仅在以下前提全部满足时才成立】：

1. 事件来自消息“实时触发”
   - 禁止延迟处理
   - 禁止历史回放
   - 禁止补偿执行

2. raw_message.time 字段：
   - 必须存在
   - 必须为秒级整数
   - 所有 Bot 观测值必须一致

3. 所有 Bot：
   - 使用完全一致的协议参数
   - 严禁任何形式的配置化

若任一前提不满足，则仲裁结果不具备一致性保证。

──────────────────────────────────────────────────────────────────────
三、协议角色定义
──────────────────────────────────────────────────────────────────────

1. 参与者（Participant）
   - 在仲裁窗口内，对目标消息贴出协议占坑表情（282）的实体
   - 不区分 Bot 或人工用户（协议假设其安全性）

2. 候选胜出者（Candidate）
   - 按确定性规则，从参与者集合中排序得到的有序列表成员

3. 实际胜出者（Winner）
   - 在其候选顺位内，成功贴出确认表情（355）的第一个候选者

──────────────────────────────────────────────────────────────────────
四、协议表情语义（不可变）
──────────────────────────────────────────────────────────────────────

1. 表情 282（占坑表情）
   - 语义：声明“我参与本轮仲裁”
   - 仅用于确定参与者集合
   - 不代表胜出，也不代表优先级

2. 表情 355（确认表情）
   - 语义：确认“我已承担胜出执行义务”
   - 不可撤销
   - 不允许补贴
   - 不允许重复贴出

355 是“胜出权存在性证明”，而非奖励或装饰。

──────────────────────────────────────────────────────────────────────
五、核心仲裁流程（状态机）
──────────────────────────────────────────────────────────────────────

对每一条消息，所有 Bot 必须严格按以下阶段执行：

Phase 1：初始窗口检测
- 若已观测到 282，则直接退出，不参与仲裁

Phase 2：占坑
- 对消息贴出 282
- 若失败，立即退出

Phase 3：仲裁窗口等待
- 等待固定时间窗口（_WAIT_SEC）

Phase 4：参与者收集
- 拉取所有贴出 282 的参与者
- 该集合一经确认，不得再变化

Phase 5：胜出顺序计算（仅一次）
- 基于 (参与者集合, 消息时间) 计算确定性有序列表
- 禁止重新计算
- 禁止重新收集参与者

Phase 6：确定性递补确认
- 按顺序遍历候选者列表
- 每一轮：
  - 当前候选者尝试贴出 355
  - 所有 Bot 在固定窗口内观测是否出现 355
- 首个成功确认 355 的候选者成为实际胜出者

若所有候选者均未贴出 355，则本轮仲裁作废。

──────────────────────────────────────────────────────────────────────
六、递补机制的协议语义
──────────────────────────────────────────────────────────────────────

1. 递补 ≠ 重新仲裁
   - 不重新 fetch
   - 不重新 decide
   - 不重新 sleep

2. 递补仅是：
   - 在同一个确定性顺序中推进指针

3. 任一候选者：
   - 崩溃
   - 超时
   - API 调用失败
   都将自动丧失胜出权，不影响后续候选者。

──────────────────────────────────────────────────────────────────────
七、全局一致性保证来源
──────────────────────────────────────────────────────────────────────

本协议的一致性仅依赖以下不变因素：

- 同一条消息
- 同一参与者集合
- 同一 msg_time
- 同一排序与递补规则
- 同一固定时间窗口

在此前提下：
- 所有 Bot 的行为路径一致
- 所有 Bot 的胜负判断一致

──────────────────────────────────────────────────────────────────────
八、明确禁止的行为（破坏协议）
──────────────────────────────────────────────────────────────────────

以下行为将直接破坏全局一致性，严禁出现：

- 重新计算胜出顺序
- 动态增删参与者
- 递补过程中重新 fetch 282
- 看到 355 后尝试“补救”贴出
- 引入任何跨事件的本地或外部状态

──────────────────────────────────────────────────────────────────────
九、协议失败语义
──────────────────────────────────────────────────────────────────────

当出现以下情况之一时，仲裁视为失败：

- 无有效参与者
- 所有候选者均未确认 355
- 协议前提不满足

失败意味着：
- 本轮仲裁无胜出者
- 所有 Bot 均不得继续执行重逻辑

──────────────────────────────────────────────────────────────────────
十、设计哲学
──────────────────────────────────────────────────────────────────────

仲裁计算的是“顺序”，
胜出是对顺序的确认，
递补是对失败的自然前移，
一致性来自确定性，而非控制力。

──────────────────────────────────────────────────────────────────────
"""

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
    基于固定表情点赞状态的弱一致分布式仲裁器（支持确定性递补）。

    仲裁特性：
    - 仲裁顺序一次性确定
    - 递补不重新仲裁，仅推进顺序指针
    - 355 作为胜出权确认信号
    """

    # ===== 协议常量（严禁配置化） =====

    _EMOJI_ID = 282
    _EMOJI_TYPE = "1"
    _WAIT_SEC = 1.0

    _FEEDBACK_EMOJI_ID = 355
    _FEEDBACK_EMOJI_TYPE = "1"
    _FEEDBACK_WAIT_SEC = 0.7

    _TIME_SLICE = 60

    # ===== 对外接口 =====

    async def compete(self, event: AiocqhttpMessageEvent) -> bool:
        ctx = self._parse_event_context(event)
        if ctx is None:
            return False

        bot = event.bot
        mid = ctx.message_id

        # Phase 1：初始窗口检测
        users = await self._fetch_users(bot, mid)
        if users:
            return False

        # Phase 2：占坑
        try:
            await bot.set_msg_emoji_like(
                message_id=mid,
                emoji_id=self._EMOJI_ID,
                set=True,
            )
        except Exception as e:
            logger.warning(
                f"[arbiter][io] set_msg_emoji_like failed: {e}", exc_info=True
            )
            return False

        # Phase 3：等待仲裁窗口
        await asyncio.sleep(self._WAIT_SEC)

        # Phase 4：收集最终参与者
        users = await self._fetch_users(bot, mid)
        if not users:
            # 极端 API 延迟兜底
            return True

        # Phase 5：一次性计算胜出顺序
        order = self._decide_order(users, ctx.msg_time)
        if not order:
            return False

        # Phase 6：按顺序递补确认
        for candidate in order:
            # 当前候选者执行胜出义务
            if candidate == ctx.self_id:
                try:
                    await bot.set_msg_emoji_like(
                        message_id=mid,
                        emoji_id=self._FEEDBACK_EMOJI_ID,
                        emoji_type=self._FEEDBACK_EMOJI_TYPE,
                        set=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"[arbiter][feedback] set 355 failed: {e}", exc_info=True
                    )

            # 等待反馈窗口
            await asyncio.sleep(self._FEEDBACK_WAIT_SEC)

            # 观测是否已有 355（不关心是谁贴的）
            if await self._has_feedback(bot, mid):
                return candidate == ctx.self_id

        # 所有候选者均未确认胜出权
        logger.warning("[arbiter][protocol] no feedback observed, abort arbitration")
        return False

    # ===== 协议前置解析 =====

    def _parse_event_context(
        self, event: AiocqhttpMessageEvent
    ) -> _ArbiterContext | None:
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

    async def _has_feedback(self, bot, message_id: int) -> bool:
        """
        判断是否观测到胜出确认信号（355）。
        """
        try:
            resp = await bot.fetch_emoji_like(
                message_id=message_id,
                emojiId=str(self._FEEDBACK_EMOJI_ID),
                emojiType=self._FEEDBACK_EMOJI_TYPE,
            )
        except Exception:
            return False

        likes = (resp or {}).get("emojiLikesList") or []
        return bool(likes)

    def _decide_order(self, users: list[int], msg_time: int) -> list[int]:
        """
        基于确定性规则生成胜出递补顺序。

        保证：
        - 顺序在所有 Bot 上完全一致
        - 不随时间推进而变化
        """
        try:
            participants = sorted(set(users))
            if not participants:
                raise ValueError("empty participants")

            base = (msg_time // self._TIME_SLICE) % len(participants)
            return [
                participants[(base + i) % len(participants)]
                for i in range(len(participants))
            ]
        except Exception as e:
            logger.warning(f"[arbiter][protocol] decide_order failed: {e}")
            return []
