# debounce.py

import time


class Debouncer:
    """
    会话级防抖器
    - 支持 link 防抖
    - 支持 resource_id 防抖
    """

    def __init__(self, config: dict):
        self.interval = config["debounce_interval"]
        self._cache: dict[str, dict[str, float]] = {}  # {session: {key: ts}}

    def _hit(self, session: str, key: str) -> bool:
        # 禁用
        if self.interval <= 0:
            return False

        now = time.time()
        bucket = self._cache.setdefault(session, {})

        # 1. 清理过期
        expire = now - self.interval
        for k, ts in list(bucket.items()):
            if ts < expire:
                bucket.pop(k, None)

        # 2. 命中判断
        if key in bucket:
            return True

        # 3. 记录
        bucket[key] = now
        return False

    def hit_link(self, session: str, link: str) -> bool:
        """基于 link 的防抖"""
        return self._hit(session, f"link:{link}")

    def hit_resource(self, session: str, resource_id: str) -> bool:
        """基于资源 ID 的防抖"""
        return self._hit(session, f"res:{resource_id}")
