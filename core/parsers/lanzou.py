from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

import aiofiles

from ..exception import ParseException
from ..utils import safe_unlink
from .base import BaseParser, handle
from .data import Platform


def _dbg(tag: str, data: Any):
    print(f"[LANZOU-DEBUG] {tag}: {data}")


class LanzouParser(BaseParser):
    """
    蓝奏云解析器：解析分享页 -> iframe -> ajaxm.php 直链 -> 下载文件返回本地路径
    调试版：输出关键上下文，便于定位 405/参数问题。
    """

    platform: ClassVar[Platform] = Platform(
        name="lanzou", display_name="蓝奏云"
    )

    @handle("lanzou", r"https?://[^/\s]*lanzou[a-z]?\.com/[^\s]+")
    async def parse_lanzou(self, searched: re.Match[str]):
        share_url = searched.group(0)
        _dbg("share_url", share_url)

        direct_url = await self._resolve_direct(share_url)
        _dbg("direct_url", direct_url)

        file_path = await self._download_file(
            share_url=share_url,
            direct_url=direct_url,
            referer_candidates=[share_url, direct_url, None],
            max_refresh=0,  # 调试阶段先不刷新直链，定位首次返回
        )
        path_task = asyncio.get_event_loop().create_task(
            asyncio.sleep(0, result=file_path)
        )
        return self.result(
            title="蓝奏云文件",
            url=share_url,
            contents=[self.create_file_content(path_task)],
        )

    # ---------------- internal helpers ---------------- #

    @staticmethod
    def _get_acw_sc__v2(arg1: str) -> str:
        pos_list = [
            0xF,
            0x23,
            0x1D,
            0x18,
            0x21,
            0x10,
            0x1,
            0x26,
            0xA,
            0x9,
            0x13,
            0x1F,
            0x28,
            0x1B,
            0x16,
            0x17,
            0x19,
            0xD,
            0x6,
            0xB,
            0x27,
            0x12,
            0x14,
            0x8,
            0xE,
            0x15,
            0x20,
            0x1A,
            0x2,
            0x1E,
            0x7,
            0x4,
            0x11,
            0x5,
            0x3,
            0x1C,
            0x22,
            0x25,
            0xC,
            0x24,
        ]
        mask = "3000176000856006061501533003690027800375"
        arg2 = [""] * len(arg1)
        for j in range(len(pos_list)):
            if j < len(arg2) and (pos_list[j] - 1) < len(arg1):
                arg2[j] = arg1[pos_list[j] - 1]
        arg2_str = "".join(arg2)
        out = []
        for i in range(0, len(arg2_str), 2):
            if i + 2 > len(mask):
                break
            s = int(arg2_str[i : i + 2], 16)
            m = int(mask[i : i + 2], 16)
            out.append(f"{s ^ m:02x}")
        return "".join(out)

    @staticmethod
    def _extract_params(content: str):
        def g(pat: str):
            m = re.search(pat, content)
            return m.group(1).strip() if m else None

        ajaxdata = g(r"ajaxdata\s*=\s*['\"]([^'\"]+)['\"]")
        wp_sign = g(r"wp_sign\s*=\s*['\"]([^'\"]+)['\"]")
        websign = g(r"websign\s*=\s*['\"]([^'\"]+)['\"]") or g(
            r"ws_sign\s*=\s*['\"]([^'\"]+)['\"]"
        )
        websignkey = g(r"websignkey\s*=\s*['\"]([^'\"]+)['\"]")
        kd = g(r"kdns\s*=\s*([0-9]+)") or g(r"kd\s*=\s*([0-9]+)")

        # file_id 多模式尝试
        file_id = (
            g(r"ajaxm\.php\?file=([0-9]+)")
            or g(r"file_id\s*=\s*['\"]?([0-9]+)['\"]?")
            or g(r"fid\s*=\s*['\"]?([0-9]+)['\"]?")
        )

        if not websignkey and ajaxdata:
            websignkey = ajaxdata
        if not websign:
            websign = ""
        if not kd:
            kd = "1"
        signs = ajaxdata

        if not wp_sign:
            return None

        return {
            "sign": wp_sign,
            "signs": signs,
            "websign": websign,
            "websignkey": websignkey,
            "kd": kd,
            "file_id": file_id,  # 可能为 None
        }

    async def _get_iframe_url(self, url: str) -> tuple[str, str]:
        resp = await self.client.get(url, headers=self.headers)
        text = await resp.text()
        if "acw_sc__v2" in text and "arg1" in text:
            arg1_m = re.search(r"arg1='([0-9A-F]+)'", text)
            if not arg1_m:
                raise ParseException("无法提取 arg1")
            acw = self._get_acw_sc__v2(arg1_m.group(1))
            self.client.cookie_jar.update_cookies({"acw_sc__v2": acw})
            resp = await self.client.get(url, headers=self.headers)
            text = await resp.text()

        iframe_match = re.search(r'<iframe.*?src="([^"]+)"', text)
        if not iframe_match:
            raise ParseException("未找到 iframe")
        iframe_path = iframe_match.group(1)
        domain = url.split("/")[2]
        iframe_url = (
            iframe_path
            if iframe_path.startswith("http")
            else f"https://{domain}{iframe_path}"
        )
        _dbg("iframe_domain", domain)
        _dbg("iframe_url", iframe_url)
        return domain, iframe_url

    async def _fetch_params(self, iframe_url: str):
        await asyncio.sleep(random.uniform(0.03, 0.06))
        resp = await self.client.get(iframe_url, headers=self.headers)
        content = await resp.text()
        _dbg("iframe_html_preview", content[:800])
        params = self._extract_params(content)
        _dbg("params_extracted", params)
        if not params:
            raise ParseException("无法提取 sign/ajaxdata")
        return params

    async def _call_api_once(
        self,
        url: str,
        iframe_url: str,
        domain: str,
        params: dict[str, Any],
        use_iframe_ref: bool,
        with_origin: bool,
    ) -> str:
        # ajaxm 备选 URL：带 file 与不带 file
        api_urls = []
        if params.get("file_id"):
            api_urls.append(f"https://{domain}/ajaxm.php?file={params['file_id']}")
        api_urls.append(f"https://{domain}/ajaxm.php")

        headers = {
            "User-Agent": self.headers["User-Agent"],
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": iframe_url if use_iframe_ref else url,
        }
        if with_origin:
            headers["Origin"] = f"https://{domain}"

        kd_candidates = [params.get("kd", "1")]
        if "0" not in kd_candidates:
            kd_candidates.append("0")

        cookies_now = {
            c.key: c.value for c in self.client.cookie_jar if domain in c["domain"]
        }
        _dbg("ajaxm_domain", domain)
        _dbg("ajaxm_headers_base", headers)
        _dbg("ajaxm_cookies_filtered", cookies_now)

        last_err: Exception | None = None
        for api_url in api_urls:
            _dbg("ajaxm_url_try", api_url)
            for kd_val in kd_candidates:
                data = {
                    "action": "downprocess",
                    "sign": params["sign"],
                    "signs": params.get("signs", ""),
                    "websign": params.get("websign", ""),
                    "websignkey": params.get("websignkey", ""),
                    "kd": kd_val,
                    "ves": 1,
                }
                _dbg(
                    "ajaxm_try",
                    {
                        "use_iframe_ref": use_iframe_ref,
                        "with_origin": with_origin,
                        "kd": kd_val,
                        "data": data,
                    },
                )
                resp = await self.client.post(api_url, headers=headers, data=data)
                body_preview = (await resp.text())[:400]
                _dbg(
                    "ajaxm_resp", {"status": resp.status, "body_preview": body_preview}
                )
                if resp.status != 200:
                    last_err = ParseException(
                        f"ajaxm HTTP {resp.status}: {body_preview}"
                    )
                    continue
                try:
                    res_json = await resp.json(content_type=None)
                except Exception:
                    try:
                        res_json = json.loads(body_preview)
                    except Exception:
                        last_err = ParseException(f"ajaxm 非 JSON: {body_preview}")
                        continue

                if res_json.get("zt") == 1 and res_json.get("url"):
                    return f"{res_json['dom']}/file/{res_json['url']}"
                last_err = ParseException(f"ajaxm 返回异常: {res_json}")

        raise last_err if last_err else ParseException("ajaxm 调用失败")

    async def _resolve_direct(self, url: str) -> str:
        combos = [
            (True, False),
            (True, True),
            (False, False),
            (False, True),
        ]
        last_err: Exception | None = None
        for _ in range(3):
            try:
                domain, iframe_url = await self._get_iframe_url(url)
                params = await self._fetch_params(iframe_url)
                for use_iframe_ref, with_origin in combos:
                    try:
                        return await self._call_api_once(
                            url, iframe_url, domain, params, use_iframe_ref, with_origin
                        )
                    except Exception as e:
                        last_err = e
                        continue
            except Exception as e:
                last_err = e
            await asyncio.sleep(random.uniform(0.05, 0.10))
        raise last_err if last_err else ParseException("未知错误")

    async def _download_file(
        self,
        share_url: str,
        direct_url: str,
        referer_candidates: list[str | None],
        max_refresh: int = 0,
    ) -> Path:
        last_err: Exception | None = None
        current_direct = direct_url
        for _ in range(max_refresh + 1):
            for ref in referer_candidates:
                headers = {**self.headers}
                if ref:
                    headers["Referer"] = ref
                _dbg("download_try", {"url": current_direct, "referer": ref})
                try:
                    return await self._download_once(current_direct, headers)
                except Exception as e:
                    _dbg("download_fail", str(e))
                    last_err = e
                    continue
        raise last_err if last_err else ParseException("下载直链失败")

    async def _download_once(self, url: str, headers: dict[str, str]) -> Path:
        async with self.client.get(url, headers=headers, allow_redirects=True) as resp:
            body_preview = (await resp.text())[:400]
            _dbg(
                "download_resp",
                {
                    "status": resp.status,
                    "ct": resp.headers.get("Content-Type"),
                    "body_preview": body_preview,
                },
            )
            if resp.status >= 400:
                raise ParseException(f"下载直链失败 HTTP {resp.status}")
            content_type = resp.headers.get("Content-Type", "")
            filename = self._guess_filename(url, resp.headers) # type: ignore
            file_path = self.downloader.cache_dir / filename

            total_written = 0
            first_chunk = b""
            async with aiofiles.open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if not chunk:
                        continue
                    if not first_chunk:
                        first_chunk = chunk[:1024]
                    await f.write(chunk)
                    total_written += len(chunk)

            if total_written == 0 or (
                "text/html" in content_type.lower() and b"<html" in first_chunk.lower()
            ):
                await safe_unlink(file_path)
                body_preview = first_chunk.decode("utf-8", "ignore")[:400]
                raise ParseException(
                    f"直链响应为空或返回 HTML，content-type={content_type}, preview={body_preview}"
                )
            return file_path

    @staticmethod
    def _guess_filename(url: str, headers: dict[str, str]) -> str:
        cd = headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=UTF-8\'\'([^;]+)", cd)
        if not m:
            m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            name = m.group(1)
        else:
            tail = url.split("?")[0].split("#")[0].split("/")[-1]
            name = tail or "lanzou_file"
        name = unquote(name)
        name = name.split("?")[0].split("#")[0]
        name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
        if not name:
            name = f"lanzou_{int(time.time())}"
        if len(name) > 80:
            name = name[:80]
        if "." not in name:
            name += ".zip"
        return name
