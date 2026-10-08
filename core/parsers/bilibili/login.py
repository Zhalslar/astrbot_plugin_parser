import asyncio
import io
import json
from collections.abc import AsyncGenerator

import qrcode
from bilibili_api import Credential
from curl_cffi.requests import AsyncSession

from astrbot.api import logger

from ...config import PluginConfig

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 轮询接口返回的状态码
QR_STATUS_SUCCESS = 0
QR_STATUS_SCAN = 86101
QR_STATUS_CONFIRM = 86090
QR_STATUS_EXPIRED = 86038

QR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

CREDENTIAL_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")


def parse_qr_login_cookies(
    payload: dict, set_cookie_headers: list[str] | None = None
) -> dict[str, str]:
    """从扫码成功的轮询响应中提取登录 Cookies

    旧接口把 Cookies 拼在 `url` 查询参数里, 新接口改为通过 `Set-Cookie` 响应头下发,
    两种格式都解析。
    """
    cookies: dict[str, str] = {}

    # 1) url 查询参数: 部分返回以 `&amp;` 拼接, 参数名会残留 `amp;` 前缀
    query = (payload.get("url") or "").partition("?")[2]
    for chunk in query.split("&"):
        name, _, value = chunk.partition("=")
        name = name.strip().removeprefix("amp;")
        if name in CREDENTIAL_KEYS and value:
            cookies.setdefault(name, value.strip())

    # 2) Set-Cookie 响应头
    for header in set_cookie_headers or []:
        name, _, value = header.partition("=")
        name = name.strip()
        if name in CREDENTIAL_KEYS and value:
            cookies.setdefault(name, value.split(";")[0].strip())

    return cookies


class BilibiliLogin:
    """哔哩哔哩登录类"""

    def __init__(self, config: PluginConfig):
        self.credential_file = config.data_dir / "cookies" / "bilibili_credential.json"
        self.raw_cookies = config.parser.bilibili.cookies
        self._credential: Credential | None = None
        self._qr_key: str | None = None

    def _save_credential(self):
        """存储哔哩哔哩登录凭证"""
        if self._credential is None:
            return
        if not self._credential.has_sessdata():
            logger.warning("哔哩哔哩凭证缺少 SESSDATA, 跳过保存")
            return

        cookies = {
            name: value
            for name, value in self._credential.get_cookies().items()
            if value
        }
        self.credential_file.parent.mkdir(parents=True, exist_ok=True)
        self.credential_file.write_text(
            json.dumps(cookies, ensure_ascii=False), encoding="utf-8"
        )

    def _load_credential(self):
        """从文件加载哔哩哔哩登录凭证"""
        if not self.credential_file.exists():
            return

        cookies = json.loads(self.credential_file.read_text())
        if "SESSDATA" not in cookies and "sessdata" in cookies:
            cookies["SESSDATA"] = cookies["sessdata"]
        if "DedeUserID" not in cookies and "dedeuserid" in cookies:
            cookies["DedeUserID"] = cookies["dedeuserid"]
        credential = Credential.from_cookies(cookies)
        if credential.has_sessdata():
            self._credential = credential
        else:
            logger.warning(f"哔哩哔哩凭证文件缺少 SESSDATA: {self.credential_file}")

    async def login_with_qrcode(self) -> bytes:
        """通过二维码登录获取哔哩哔哩登录凭证"""
        async with AsyncSession(impersonate="chrome131", headers=QR_HEADERS) as session:
            resp = await session.get(QR_GENERATE_URL, timeout=15)
            payload = (resp.json() or {}).get("data") or {}

        url = payload.get("url")
        qr_key = payload.get("qrcode_key")
        if not url or not qr_key:
            raise ValueError(f"获取哔哩哔哩登录二维码失败: {payload}")

        self._qr_key = qr_key
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2
        )
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        return buffer.getvalue()

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """检查二维码登录状态"""
        if not self._qr_key:
            yield "未找到二维码, 请重新生成"
            return

        scan_tip_pending = True

        async with AsyncSession(impersonate="chrome131", headers=QR_HEADERS) as session:
            for _ in range(30):
                resp = await session.get(
                    QR_POLL_URL, params={"qrcode_key": self._qr_key}, timeout=15
                )
                payload = (resp.json() or {}).get("data") or {}
                status = payload.get("code")

                if status == QR_STATUS_SUCCESS:
                    cookies = parse_qr_login_cookies(
                        payload, resp.headers.get_list("set-cookie")
                    )
                    if not cookies.get("SESSDATA"):
                        yield "登录成功, 但未能提取 SESSDATA, 请重新扫码"
                        return
                    logger.debug(
                        "哔哩哔哩扫码登录: SESSDATA 取自 %s",
                        "url 参数"
                        if "SESSDATA=" in (payload.get("url") or "")
                        else "Set-Cookie 响应头",
                    )
                    self._credential = Credential(
                        sessdata=cookies["SESSDATA"],
                        bili_jct=cookies.get("bili_jct", ""),
                        dedeuserid=cookies.get("DedeUserID", ""),
                        ac_time_value=payload.get("refresh_token") or "",
                    )
                    self._save_credential()
                    yield "登录成功"
                    return

                if status == QR_STATUS_CONFIRM:
                    if scan_tip_pending:
                        yield "二维码已扫描, 请确认登录"
                        scan_tip_pending = False
                elif status == QR_STATUS_EXPIRED:
                    yield "二维码过期, 请重新生成"
                    return
                await asyncio.sleep(2)

        yield "二维码登录超时, 请重新生成"

    def _cookies_to_dict(self, cookies_str: str) -> dict[str, str]:
        """将 cookies 字符串转换为字典"""
        res = {}
        for cookie in cookies_str.split(";"):
            name, value = cookie.strip().split("=", 1)
            res[name] = value
        return res

    async def _init_credential(self):
        """初始化哔哩哔哩登录凭证"""
        if not self.raw_cookies:
            self._load_credential()
            return

        credential = Credential.from_cookies(self._cookies_to_dict(self.raw_cookies))
        try:
            ck_valid = await credential.check_valid()
        except Exception:
            # 检查接口偶发异常(如 -101), 按未通过处理, 回退到凭证文件
            ck_valid = False
        if ck_valid:
            logger.info(f"`parser_bili_ck` 有效, 保存到 {self.credential_file}")
            self._credential = credential
            self._save_credential()
        else:
            logger.info(f"`parser_bili_ck` 已过期, 尝试从 {self.credential_file} 加载")
            self._load_credential()

    @property
    async def credential(self) -> Credential | None:
        """哔哩哔哩登录凭证"""

        if self._credential is None:
            await self._init_credential()
            return self._credential

        if not self._credential.has_sessdata():
            self._load_credential()
            if self._credential is None or not self._credential.has_sessdata():
                logger.warning("哔哩哔哩凭证缺少 SESSDATA, 请重新登录")
                return None

        try:
            cv_valid = await self._credential.check_valid()
        except Exception:
            # 检查接口偶发异常, 按未通过处理
            cv_valid = False
        if not cv_valid:
            # bilibili-api 的 check_valid 对有效凭证也会间歇性返回 False
            # (接口误报), 同一凭证往往仍可正常解析, 告警放行而非直接拦截
            logger.warning("哔哩哔哩凭证 check_valid 未通过(可能为接口误报, 跳过拦截继续使用)")

        try:
            need_refresh = await self._credential.check_refresh()
        except Exception:
            # check_refresh 对失效/被风控凭证会抛 ResponseCodeException(-101),
            # 异常时跳过刷新检查, 不中断解析
            need_refresh = False
        if need_refresh:
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                logger.info(f"哔哩哔哩凭证刷新成功, 保存到 {self.credential_file}")
                self._save_credential()
            else:
                logger.warning(
                    "哔哩哔哩凭证刷新需要包含 `SESSDATA`, `ac_time_value` 项"
                )

        return self._credential
