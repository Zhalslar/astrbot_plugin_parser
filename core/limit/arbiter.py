import asyncio
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class EmojiLikeArbiter:
    """
    使用固定 282 表情的延迟仲裁器

    规则：
    1. 先 fetch_emoji_like 看是否有人贴 282
    2. 有人贴 → 不再贴
    3. 没人贴 → 自己贴一个 282
    4. 等待 wait_sec
    5. 再 fetch_emoji_like
    6. 用「当前小时 + 用户集合」决定唯一赢家
    """

    EMOJI_ID = 282
    EMOJI_TYPE = "1"

    def __init__(self, config: AstrBotConfig):
        self.wait_sec: float = float(config["arbiter_wait_sec"])

    async def compete(
        self,
        bot,
        *,
        message_id: int,
        self_id: int,
    ) -> bool:
        """
        参与仲裁

        :return:
            True  -> 本 Bot 负责解析
            False -> 放弃解析
        """
        # 第一次检查：是否已经有人贴了，有人贴了就放弃
        users = await self._fetch_users(bot, message_id)
        if users:
            logger.debug(
                f"[arbiter] 消息({message_id})已有人贴 {self.EMOJI_ID}：{users}"
            )
            return False

        # 没人贴，尝试自己贴一个
        try:
            await bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=self.EMOJI_ID,
                set=True,
            )
            logger.debug(
                f"[arbiter] Bot({self_id}) 给消息({message_id})贴了 {self.EMOJI_ID}"
            )
        except Exception as e:
            logger.warning(f"[arbiter] Bot({self_id}) 贴 {self.EMOJI_ID} 失败：{e}")
            return False

        # 等待其他 Bot / 用户反应
        await asyncio.sleep(self.wait_sec)

        # 第二次检查
        users = await self._fetch_users(bot, message_id)
        if not users:
            logger.warning(
                f"[arbiter] 消息({message_id}) 等待后仍无人贴 {self.EMOJI_ID}，API 可能未及时反映 Bot 的操作，视为成功"
            )
            return True

        return self._decide(users, self_id)

    async def _fetch_users(self, bot, message_id: int) -> list[int]:
        """
        获取所有给该消息贴了表情的用户 tinyId
        """
        try:
            resp = await bot.fetch_emoji_like(
                message_id=message_id,
                emojiId=str(self.EMOJI_ID),
                emojiType=self.EMOJI_TYPE,
            )
        except Exception as e:
            logger.warning(f"[arbiter] fetch_emoji_like 失败：{e}")
            return []

        lst = resp.get("emojiLikesList") or []
        users: list[int] = []

        for item in lst:
            try:
                users.append(int(item["tinyId"]))
            except Exception:
                continue

        return users

    def _decide(self, users: list[int], self_id: int) -> bool:
        """
        根据映射规则判断自己是否胜出
        """
        try:
            users = sorted(set(users))
            if not users:
                raise ValueError("empty user_ids")

            hour = int(datetime.now(timezone.utc).timestamp() // 3600)
            winner = users[hour % len(users)]
        except Exception as e:
            logger.warning(f"[arbiter] 决策失败：{e}")
            return False

        logger.debug(f"[arbiter] 参与者={users}，赢家={winner}")
        return winner == self_id
