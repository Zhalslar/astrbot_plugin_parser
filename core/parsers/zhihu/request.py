from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from astrbot.api import logger

from ...exception import ParseException
from .common import RequestContext


class ZhihuRequestMixin:
    async def _fetch_initial_data(
        self,
        url: str,
        *,
        validator: Callable[[dict[str, Any]], bool],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        last_error: Exception | None = None
        saw_challenge = False
        saw_login = False
        saw_invalid_target = False
        saw_initial_data = False

        for profile_name, profile_url, headers, impersonate in self._request_profiles(
            url
        ):
            try:
                response_ctx = await self._request_text(
                    profile_url,
                    headers=headers,
                    impersonate=impersonate,
                )

                html_text = str(response_ctx["text"])
                final_url = str(response_ctx["final_url"])
                status_code = int(response_ctx["status_code"])

                if self._is_challenge_page(html_text, status_code=status_code):
                    saw_challenge = True
                    logger.debug(
                        f"[知乎] {profile_name} 命中反爬挑战页: {profile_url} -> {final_url}"
                    )
                    continue

                if self._is_login_page(final_url, html_text):
                    saw_login = True
                    logger.debug(
                        f"[知乎] {profile_name} 命中登录页: {profile_url} -> {final_url}"
                    )
                    continue

                initial_data = self._extract_initial_data(html_text)
                if not initial_data:
                    logger.debug(
                        f"[知乎] {profile_name} 未找到可解析 initialData: "
                        f"{profile_url} -> {final_url}, status={status_code}"
                    )
                    continue

                saw_initial_data = True
                if validator(initial_data):
                    logger.debug(
                        f"[知乎] 使用 {profile_name} 请求成功: {profile_url} -> {final_url}"
                    )
                    return initial_data, headers

                saw_invalid_target = True
                logger.debug(
                    f"[知乎] {profile_name} 拿到的页面不是目标页: "
                    f"{profile_url} -> {final_url}, status={status_code}"
                )
            except Exception as exc:
                last_error = exc
                logger.debug(
                    f"[知乎] {profile_name} 请求失败: {profile_url}, error={exc}"
                )

        if saw_challenge:
            if self.mycfg.cookies:
                raise ParseException(
                    "知乎抓取失败：当前 cookies 可能失效，或请求仍被风控拦截"
                )
            raise ParseException("知乎抓取失败：站点返回反爬挑战页，请配置有效 cookies")

        if saw_login:
            if self.mycfg.cookies:
                raise ParseException("知乎抓取失败：当前 cookies 可能失效，或权限不足")
            raise ParseException(
                "知乎抓取失败：当前请求被引导到登录页，请配置有效 cookies"
            )

        if saw_invalid_target or saw_initial_data:
            raise ParseException("知乎抓取失败：未拿到目标知乎页面")

        raise ParseException("知乎页面抓取失败") from last_error

    async def _fetch_json_data(
        self,
        url: str,
        *,
        validator: Callable[[dict[str, Any]], bool],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        last_error: Exception | None = None
        saw_challenge = False
        saw_forbidden = False
        saw_login = False
        saw_invalid_target = False
        saw_json_payload = False

        for profile_name, profile_url, headers, impersonate in self._request_profiles(
            url,
            accept="application/json, text/plain, */*",
        ):
            try:
                response_ctx = await self._request_text(
                    profile_url,
                    headers=headers,
                    impersonate=impersonate,
                )

                body_text = str(response_ctx["text"])
                final_url = str(response_ctx["final_url"])
                status_code = int(response_ctx["status_code"])
                content_type = str(response_ctx.get("content_type") or "")

                if self._is_challenge_page(body_text, status_code=status_code):
                    saw_challenge = True
                    logger.debug(
                        f"[知乎] {profile_name} 命中反爬挑战接口: {profile_url} -> {final_url}"
                    )
                    continue

                if self._is_login_page(final_url, body_text):
                    saw_login = True
                    logger.debug(
                        f"[知乎] {profile_name} 命中登录接口: {profile_url} -> {final_url}"
                    )
                    continue

                if status_code in (401, 403):
                    saw_forbidden = True
                    logger.debug(
                        f"[知乎] {profile_name} JSON 接口无权限: "
                        f"{profile_url} -> {final_url}, status={status_code}"
                    )
                    continue

                if status_code >= 400:
                    saw_invalid_target = True
                    logger.debug(
                        f"[知乎] {profile_name} JSON 接口状态异常: "
                        f"{profile_url} -> {final_url}, status={status_code}"
                    )
                    continue

                payload = self._extract_json_payload(
                    body_text,
                    content_type=content_type,
                )
                if payload is None:
                    logger.debug(
                        f"[知乎] {profile_name} 未返回可解析 JSON: "
                        f"{profile_url} -> {final_url}, content-type={content_type}"
                    )
                    continue

                saw_json_payload = True
                if validator(payload):
                    logger.debug(
                        f"[知乎] 使用 {profile_name} JSON 接口请求成功: "
                        f"{profile_url} -> {final_url}"
                    )
                    return payload, headers

                saw_invalid_target = True
                logger.debug(
                    f"[知乎] {profile_name} JSON 接口拿到的不是目标数据: "
                    f"{profile_url} -> {final_url}, status={status_code}"
                )
            except Exception as exc:
                last_error = exc
                logger.debug(
                    f"[知乎] {profile_name} JSON 接口请求失败: {profile_url}, error={exc}"
                )

        if saw_challenge or saw_forbidden:
            if self.mycfg.cookies:
                raise ParseException(
                    "知乎抓取失败：当前 cookies 可能失效，或请求仍被风控拦截"
                )
            raise ParseException("知乎抓取失败：站点返回反爬挑战页，请配置有效 cookies")

        if saw_login:
            if self.mycfg.cookies:
                raise ParseException("知乎抓取失败：当前 cookies 可能失效，或权限不足")
            raise ParseException(
                "知乎抓取失败：当前请求被引导到登录页，请配置有效 cookies"
            )

        if saw_invalid_target or saw_json_payload:
            raise ParseException("知乎抓取失败：未拿到目标知乎数据")

        raise ParseException("知乎接口请求失败") from last_error

    def _request_profiles(
        self,
        url: str,
        *,
        accept: str | None = None,
    ) -> list[tuple[str, str, dict[str, str], str]]:
        desktop_headers = self._build_request_headers(self.headers, accept=accept)
        ios_headers = self._build_request_headers(self.ios_headers, accept=accept)
        mobile_headers = self._build_request_headers(
            self.android_headers, accept=accept
        )
        return [
            ("desktop", url, desktop_headers, "chrome"),
            ("ios", url, ios_headers, "safari_ios"),
            ("mobile", url, mobile_headers, "chrome_android"),
        ]

    def _build_request_headers(
        self,
        base_headers: dict[str, str],
        *,
        accept: str | None = None,
    ) -> dict[str, str]:
        headers = dict(base_headers)
        headers.update(
            {
                "accept": accept or self.headers["accept"],
                "accept-language": self.headers["accept-language"],
                "referer": self.headers["referer"],
                "origin": self.headers["origin"],
                "cache-control": self.headers["cache-control"],
                "pragma": self.headers["pragma"],
            }
        )
        if self.mycfg.cookies:
            headers["cookie"] = str(self.mycfg.cookies).strip()
        return headers

    async def _request_text(
        self,
        url: str,
        *,
        headers: dict[str, str],
        impersonate: str,
    ) -> RequestContext:
        def do_request():
            return curl_requests.get(
                url,
                headers=headers,
                impersonate=impersonate,
                proxies={"https": self.proxy, "http": self.proxy}
                if self.proxy
                else None,
                timeout=self.cfg.common_timeout,
                allow_redirects=True,
            )

        response = await asyncio.to_thread(do_request)
        return {
            "status_code": int(response.status_code),
            "final_url": str(response.url),
            "text": str(response.text),
            "content_type": str(response.headers.get("content-type", "")),
        }

    @staticmethod
    def _extract_json_payload(
        raw_text: str,
        *,
        content_type: str,
    ) -> dict[str, Any] | None:
        text = raw_text.strip()
        if not text:
            return None
        if "application/json" not in content_type.lower() and not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _extract_initial_data(self, html_text: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html_text, "html.parser")
        node = soup.select_one('script#js-initialData[type="text/json"]')
        if node is None:
            return None
        raw = node.get_text(strip=True)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        initial_state = payload.get("initialState")
        return payload if isinstance(initial_state, dict) else None

    @staticmethod
    def _entities(initial_data: dict[str, Any]) -> dict[str, Any]:
        initial_state = initial_data.get("initialState") or {}
        entities = initial_state.get("entities") or {}
        return entities if isinstance(entities, dict) else {}

    def _has_article_entity(
        self, initial_data: dict[str, Any], article_id: str
    ) -> bool:
        article = (self._entities(initial_data).get("articles") or {}).get(article_id)
        return isinstance(article, dict)

    def _has_answer_entities(
        self,
        initial_data: dict[str, Any],
        question_id: str,
        answer_id: str,
    ) -> bool:
        entities = self._entities(initial_data)
        question = (entities.get("questions") or {}).get(question_id)
        answer = (entities.get("answers") or {}).get(answer_id)
        return isinstance(question, dict) and isinstance(answer, dict)

    def _has_question_entity(
        self, initial_data: dict[str, Any], question_id: str
    ) -> bool:
        question = (self._entities(initial_data).get("questions") or {}).get(
            question_id
        )
        return isinstance(question, dict) and bool(
            self._pick_first_answer_id(initial_data, question_id)
        )

    def _has_pin_payload(self, payload: dict[str, Any], pin_id: str) -> bool:
        current_id = payload.get("id") or payload.get("pin_id") or payload.get("pinId")
        if current_id is not None and str(current_id) == pin_id:
            return True
        return any(
            payload.get(key) is not None
            for key in (
                "content_html",
                "contentHtml",
                "content",
                "author",
                "created_time",
                "updated_time",
            )
        )

    @staticmethod
    def _is_challenge_page(html_text: str, *, status_code: int) -> bool:
        lowered = html_text.lower()
        return (
            'id="zh-zse-ck"' in lowered
            or "static.zhihu.com/zse-ck/" in lowered
            or 'appname":"zse_ck"' in lowered
            or (status_code == 403 and "zse-ck" in lowered)
        )

    @staticmethod
    def _is_login_page(final_url: str, html_text: str) -> bool:
        lowered_url = final_url.lower()
        lowered_html = html_text.lower()
        return (
            "/signin" in lowered_url
            or "/signup" in lowered_url
            or "<title>知乎 - 有问题，就会有答案</title>" in lowered_html
        )

    def _pick_first_answer_id(
        self, initial_data: dict[str, Any], question_id: str
    ) -> str | None:
        initial_state = initial_data.get("initialState") or {}
        answers = ((initial_state.get("question") or {}).get("answers") or {}).get(
            question_id
        ) or {}
        ids = answers.get("ids") or []
        if not ids or not isinstance(ids[0], dict):
            return None
        target = ids[0].get("target")
        return str(target) if target else None

    async def _load_answer_for_question(
        self,
        *,
        question_id: str,
        answer_id: str,
        question_data: dict[str, Any],
        question_headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
        answer = (self._entities(question_data).get("answers") or {}).get(
            answer_id
        ) or {}
        if (
            isinstance(answer, dict)
            and answer.get("content")
            and not answer.get("contentNeedTruncated")
        ):
            return answer, question_headers, question_data

        answer_data, answer_headers = await self._fetch_initial_data(
            self._answer_url(question_id, answer_id),
            validator=lambda payload: self._has_answer_entities(
                payload,
                question_id,
                answer_id,
            ),
        )
        answer = (self._entities(answer_data).get("answers") or {}).get(answer_id) or {}
        if not isinstance(answer, dict):
            raise ParseException("知乎首条回答数据不存在")
        return answer, answer_headers, answer_data

    @staticmethod
    def _article_url(article_id: str) -> str:
        return f"https://zhuanlan.zhihu.com/p/{article_id}"

    @staticmethod
    def _pin_url(pin_id: str) -> str:
        return f"https://www.zhihu.com/pin/{pin_id}"

    @staticmethod
    def _pin_api_url(pin_id: str) -> str:
        return (
            "https://www.zhihu.com/api/v4/pins/"
            f"{pin_id}?include=content,content_html,created_time,updated_time,author,origin_pin"
        )

    @staticmethod
    def _answer_url(question_id: str, answer_id: str) -> str:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"

    @staticmethod
    def _question_url(question_id: str) -> str:
        return f"https://www.zhihu.com/question/{question_id}"
