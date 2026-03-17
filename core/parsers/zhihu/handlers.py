from __future__ import annotations

import re

from ...exception import ParseException
from ..base import handle


class ZhihuHandlerMixin:
    @handle(
        "zhuanlan.zhihu.com/p/",
        r"zhuanlan\.zhihu\.com/p/(?P<article_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_article(self, searched: re.Match[str]):
        return await self.parse_article(searched.group("article_id"))

    @handle(
        "/answer/",
        r"www\.zhihu\.com/question/(?P<question_id>\d+)/answer/(?P<answer_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_answer(self, searched: re.Match[str]):
        return await self.parse_answer(
            searched.group("question_id"),
            searched.group("answer_id"),
        )

    @handle(
        "www.zhihu.com/question/",
        r"www\.zhihu\.com/question/(?P<question_id>\d+)(?!\d)(?!/answer)(?:[/?#][^\s]*)?",
    )
    async def _parse_question(self, searched: re.Match[str]):
        return await self.parse_question(searched.group("question_id"))

    @handle(
        "www.zhihu.com/pin/",
        r"www\.zhihu\.com/pin/(?P<pin_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_pin(self, searched: re.Match[str]):
        return await self.parse_pin(searched.group("pin_id"))

    async def parse_article(self, article_id: str):
        url = self._article_url(article_id)
        initial_data, request_headers = await self._fetch_initial_data(
            url,
            validator=lambda payload: self._has_article_entity(payload, article_id),
        )
        article = (self._entities(initial_data).get("articles") or {}).get(article_id)
        if not isinstance(article, dict):
            raise ParseException("知乎文章数据不存在")

        author = self._build_author(article.get("author"), headers=request_headers)
        body_text, body_blocks, video_entries = await self._extract_content(
            str(article.get("content") or ""),
            initial_data,
            page_url=url,
        )
        article_excerpt = str(article.get("excerpt") or "")
        card_text = self._build_card_summary(
            article_excerpt,
            self._first_text_block(body_blocks),
            body_text,
        )
        ordered_blocks = self._build_section_blocks(None, body_blocks, body_text)
        stats = self._build_content_stats(
            article.get("voteupCount"),
            article.get("commentCount"),
            article.get("favlistsCount") or article.get("favoriteCount"),
            article.get("likedCount"),
            labels=("赞同", "评论", "收藏", "喜欢"),
        )
        header_text = self._compose_article_send_header(article, author)
        contents, send_groups = self._build_contents_and_groups(
            header_text,
            ordered_blocks,
            video_entries,
            request_headers=request_headers,
        )

        return self.result(
            title=str(article.get("title") or "知乎文章"),
            text=card_text,
            author=author,
            timestamp=self._safe_int(article.get("created")),
            url=url,
            contents=contents,
            send_groups=send_groups,
            extra={"info": self._build_article_card_meta(article, stats)},
        )

    async def parse_answer(self, question_id: str, answer_id: str):
        url = self._answer_url(question_id, answer_id)
        initial_data, request_headers = await self._fetch_initial_data(
            url,
            validator=lambda payload: self._has_answer_entities(
                payload,
                question_id,
                answer_id,
            ),
        )
        entities = self._entities(initial_data)
        answer = (entities.get("answers") or {}).get(answer_id)
        question = (entities.get("questions") or {}).get(question_id)
        if not isinstance(answer, dict):
            raise ParseException("知乎回答数据不存在")
        if not isinstance(question, dict):
            raise ParseException("知乎问题数据不存在")

        author = self._build_author(answer.get("author"), headers=request_headers)
        body_text, body_blocks, answer_videos = await self._extract_content(
            str(answer.get("content") or ""),
            initial_data,
            page_url=url,
        )
        answer_excerpt = str(answer.get("excerpt") or "")
        card_text = self._build_card_summary(
            answer_excerpt,
            self._first_text_block(body_blocks),
            body_text,
        )
        answer_stats = self._build_content_stats(
            answer.get("voteupCount"),
            answer.get("commentCount"),
            answer.get("favlistsCount") or answer.get("favoriteCount"),
            answer.get("thanksCount") or answer.get("likedCount"),
            labels=("赞同", "评论", "收藏", "喜欢"),
        )
        header_text = self._compose_answer_send_header(
            question=question,
            author=author,
            answer=answer,
        )
        ordered_blocks = self._build_section_blocks("回答正文:", body_blocks, body_text)
        contents, send_groups = self._build_contents_and_groups(
            header_text,
            ordered_blocks,
            answer_videos,
            request_headers=request_headers,
        )

        return self.result(
            title=str(question.get("title") or "知乎回答"),
            text=card_text,
            author=author,
            timestamp=self._safe_int(answer.get("createdTime")),
            url=url,
            contents=contents,
            send_groups=send_groups,
            extra={"info": self._build_answer_card_meta(answer_stats)},
        )

    async def parse_question(self, question_id: str):
        url = self._question_url(question_id)
        initial_data, request_headers = await self._fetch_initial_data(
            url,
            validator=lambda payload: self._has_question_entity(payload, question_id),
        )
        question = (self._entities(initial_data).get("questions") or {}).get(
            question_id
        )
        if not isinstance(question, dict):
            raise ParseException("知乎问题数据不存在")

        answer_id = self._pick_first_answer_id(initial_data, question_id)
        if not answer_id:
            raise ParseException("知乎问题页未找到默认排序首条回答")

        answer, answer_headers, answer_data = await self._load_answer_for_question(
            question_id=question_id,
            answer_id=answer_id,
            question_data=initial_data,
            question_headers=request_headers,
        )
        author = self._build_author(answer.get("author"), headers=answer_headers)

        question_detail_html = str(question.get("detail") or "").strip()
        detail_text = ""
        if question_detail_html:
            detail_text, _, _ = await self._extract_content(
                question_detail_html,
                initial_data,
                page_url=url,
                include_state_videos=False,
            )

        answer_excerpt = str(answer.get("excerpt") or "")
        answer_text, answer_blocks, answer_videos = await self._extract_content(
            str(answer.get("content") or ""),
            answer_data,
            page_url=self._answer_url(question_id, answer_id),
        )
        answer_text = answer_text or self._normalize_text(answer_excerpt)

        question_stats = self._build_question_stats(question)
        card_text = self._build_card_summary(
            question_detail_html,
            answer_excerpt,
            self._first_text_block(answer_blocks),
            answer_text,
        )
        header_text = self._compose_question_send_header(
            question=question,
            author=author,
            answer=answer,
        )
        ordered_blocks = self._build_section_blocks(
            "默认排序首条回答:",
            answer_blocks,
            answer_text,
        )
        contents, send_groups = self._build_contents_and_groups(
            header_text,
            ordered_blocks,
            answer_videos,
            request_headers=answer_headers,
        )

        return self.result(
            title=str(question.get("title") or "知乎问题"),
            text=card_text,
            author=author,
            timestamp=self._safe_int(answer.get("createdTime")),
            url=url,
            contents=contents,
            send_groups=send_groups,
            extra={"info": self._build_question_card_meta(question_stats)},
        )

    async def parse_pin(self, pin_id: str):
        url = self._pin_url(pin_id)
        payload, request_headers = await self._fetch_json_data(
            self._pin_api_url(pin_id),
            validator=lambda data: self._has_pin_payload(data, pin_id),
        )

        author = self._build_author(payload.get("author"), headers=request_headers)
        body_html = self._pin_content_html(payload)
        body_text, body_blocks, video_entries = await self._extract_content(
            body_html,
            payload,
            page_url=url,
        )
        body_text = body_text or self._pin_plain_text(payload)
        card_title = (
            self._build_card_summary(
                body_html,
                self._first_text_block(body_blocks),
                body_text,
            )
            or "知乎想法"
        )
        header_text = self._compose_pin_send_header(payload, author)
        ordered_blocks = self._build_section_blocks(None, body_blocks, body_text)
        contents, send_groups = self._build_contents_and_groups(
            header_text,
            ordered_blocks,
            video_entries,
            request_headers=request_headers,
        )

        return self.result(
            title=card_title,
            text=None,
            author=author,
            timestamp=self._pin_timestamp(payload),
            url=url,
            contents=contents,
            send_groups=send_groups,
            extra={"info": self._build_pin_card_meta(payload)},
        )
