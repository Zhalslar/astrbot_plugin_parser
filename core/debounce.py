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
        self._cleanup(bucket, now)

        if key in bucket:
            return True

        bucket[key] = now
        return False

    def _cleanup(self, bucket: dict[str, float], now: float) -> None:
        expire = now - self.interval
        for k, ts in list(bucket.items()):
            if ts < expire:
                bucket.pop(k, None)

    def _check(self, session: str, key: str) -> bool:
        if self.interval <= 0:
            return False
        now = time.time()
        bucket = self._cache.setdefault(session, {})
        self._cleanup(bucket, now)
        return key in bucket

    def _mark(self, session: str, key: str) -> None:
        if self.interval <= 0:
            return
        now = time.time()
        bucket = self._cache.setdefault(session, {})
        self._cleanup(bucket, now)
        bucket[key] = now

    def hit_link(self, session: str, link: str) -> bool:
        """基于 link 的防抖"""
        return self._hit(session, f"link:{link}")

    def hit_resource(self, session: str, resource_id: str) -> bool:
        """基于资源 ID 的防抖"""
        return self._hit(session, f"res:{resource_id}")

    def is_resource_hit(self, session: str, resource_id: str) -> bool:
        """检查资源 ID 是否命中防抖（不记录）"""
        return self._check(session, f"res:{resource_id}")

    def mark_resource(self, session: str, resource_id: str) -> None:
        """记录资源 ID 防抖"""
        self._mark(session, f"res:{resource_id}")
