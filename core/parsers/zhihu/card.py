from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .common import BodyBlock, StatItem


class ZhihuCardMixin:
    def _build_author(self, author_data: Any, *, headers: dict[str, str]):
        if not isinstance(author_data, dict):
            return None
        name = str(author_data.get("name") or "").strip()
        if not name:
            return None
        avatar_url = (
            str(
                author_data.get("avatarUrl") or author_data.get("avatar_url") or ""
            ).strip()
            or None
        )
        description = (
            self._normalize_text(
                str(author_data.get("headline") or author_data.get("description") or "")
            )
            or None
        )
        return self.create_author(
            name=name,
            avatar_url=avatar_url,
            description=description,
            headers=headers,
        )

    def _build_question_stats(self, question: dict[str, Any]) -> list[StatItem]:
        stats: list[StatItem] = []
        for label, value in (
            ("回答", question.get("answerCount")),
            ("关注", question.get("followerCount")),
            ("浏览", question.get("visitCount")),
        ):
            if value is not None:
                stats.append((label, self._format_count(value)))
        return stats

    def _build_content_stats(
        self,
        voteup: Any,
        comment: Any,
        favorite: Any,
        liked: Any,
        *,
        labels: tuple[str, str, str, str],
    ) -> list[StatItem]:
        stats: list[StatItem] = []
        for label, value in zip(
            labels, (voteup, comment, favorite, liked), strict=True
        ):
            if value is not None:
                stats.append((label, self._format_count(value)))
        return stats

    def _build_article_card_meta(
        self,
        article: dict[str, Any],
        stats: list[StatItem],
    ) -> str | None:
        tokens: list[str] = []
        if column_title := self._truncate_card_token(
            self._article_column_title(article),
            limit=12,
        ):
            tokens.append(column_title)
        for label in ("赞同", "评论", "收藏"):
            if token := self._stat_token(stats, label):
                tokens.append(token)
        return self._build_card_meta("文章", *tokens, max_tokens=5)

    def _build_answer_card_meta(self, stats: list[StatItem]) -> str | None:
        tokens = [
            token
            for label in ("赞同", "评论", "收藏")
            if (token := self._stat_token(stats, label))
        ]
        return self._build_card_meta("回答", *tokens, max_tokens=4)

    def _build_question_card_meta(self, stats: list[StatItem]) -> str | None:
        tokens = [
            token
            for label in ("回答", "关注", "浏览")
            if (token := self._stat_token(stats, label))
        ]
        return self._build_card_meta("问题", *tokens, max_tokens=4)

    def _build_pin_card_meta(self, pin: dict[str, Any]) -> str | None:
        tokens: list[str] = []
        if voteup := self._pin_stat_token(pin, "赞同", "voteup_count", "voteupCount"):
            tokens.append(voteup)
        if comment := self._pin_stat_token(
            pin,
            "评论",
            "comment_count",
            "commentCount",
        ):
            tokens.append(comment)
        return self._build_card_meta("想法", *tokens, max_tokens=3)

    def _build_card_summary(self, *sources: Any) -> str | None:
        for source in sources:
            summary = self._clean_card_summary_source(source)
            if summary:
                return self._truncate_card_summary(summary)
        return None

    def _clean_card_summary_source(self, source: Any) -> str:
        if source is None:
            return ""
        value = str(source).strip()
        if not value:
            return ""
        text = (
            self._html_to_text(value)
            if self._looks_like_html(value)
            else self._normalize_text(value)
        )
        return self._normalize_text(
            self._strip_card_prefix(text),
            keep_newlines=False,
        )

    def _truncate_card_summary(self, text: str) -> str:
        value = self._normalize_text(text, keep_newlines=False)
        if len(value) <= self._CARD_SUMMARY_LIMIT:
            return value

        window = value[: self._CARD_SUMMARY_LIMIT + 1]
        last_break = max(
            (window.rfind(marker) for marker in self._CARD_SENTENCE_MARKERS),
            default=-1,
        )
        if last_break >= max(18, self._CARD_SUMMARY_LIMIT // 2):
            return window[: last_break + 1].strip()
        return value[: self._CARD_SUMMARY_LIMIT].rstrip(" ，,；;。！？!?、") + "…"

    def _build_card_meta(
        self,
        kind: str,
        *tokens: str,
        max_tokens: int,
    ) -> str | None:
        items = [kind, *(token for token in tokens if token)]
        items = items[:max_tokens]
        return " · ".join(items) if items else None

    @staticmethod
    def _looks_like_html(text: str) -> bool:
        return bool(re.search(r"<[A-Za-z!/][^>]*>", text))

    @staticmethod
    def _strip_card_prefix(text: str) -> str:
        value = text.strip()
        for prefix in (
            "问题描述:",
            "回答正文:",
            "默认排序首条回答:",
            "专栏:",
            "标题:",
            "问题:",
        ):
            if value.startswith(prefix):
                return value[len(prefix) :].strip()
        return value

    def _first_text_block(self, body_blocks: list[BodyBlock]) -> str | None:
        for block in body_blocks:
            if block.get("kind") != "text":
                continue
            value = self._clean_card_summary_source(block.get("value"))
            if value:
                return value
        return None

    @staticmethod
    def _truncate_card_token(value: str | None, *, limit: int) -> str | None:
        if not value:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    @staticmethod
    def _stat_token(stats: list[StatItem], label: str) -> str | None:
        for current_label, value in stats:
            if current_label == label and value:
                return f"{label} {value}"
        return None

    def _pin_stat_token(
        self,
        pin: dict[str, Any],
        label: str,
        *keys: str,
    ) -> str | None:
        for key in keys:
            value = pin.get(key)
            if value is None:
                continue
            return f"{label} {self._format_count(value)}"
        return None

    def _compose_article_send_header(
        self,
        article: dict[str, Any],
        author: Any,
    ) -> str:
        sections: list[str] = []
        if title := self._normalize_text(str(article.get("title") or "")):
            sections.append(f"标题: {title}")
        sections.extend(self._author_sections(author, label="作者"))
        if column_title := self._article_column_title(article):
            sections.append(f"专栏: {column_title}")
        if created_text := self._format_timestamp(article.get("created")):
            sections.append(f"发布时间: {created_text}")
        return self._join_sections(sections)

    def _compose_answer_send_header(
        self,
        *,
        question: dict[str, Any],
        author: Any,
        answer: dict[str, Any],
    ) -> str:
        sections: list[str] = []
        if title := self._normalize_text(str(question.get("title") or "")):
            sections.append(f"问题: {title}")
        sections.extend(self._author_sections(author, label="答主"))
        if created_text := self._format_timestamp(answer.get("createdTime")):
            sections.append(f"回答时间: {created_text}")
        return self._join_sections(sections)

    def _compose_question_send_header(
        self,
        *,
        question: dict[str, Any],
        author: Any,
        answer: dict[str, Any],
    ) -> str:
        sections: list[str] = []
        if title := self._normalize_text(str(question.get("title") or "")):
            sections.append(f"问题: {title}")
        sections.extend(self._author_sections(author, label="首条回答作者"))
        if created_text := self._format_timestamp(answer.get("createdTime")):
            sections.append(f"首条回答时间: {created_text}")
        return self._join_sections(sections)

    def _compose_pin_send_header(self, pin: dict[str, Any], author: Any) -> str:
        sections: list[str] = []
        sections.extend(self._author_sections(author, label="作者"))
        if created_text := self._format_timestamp(
            pin.get("created_time") or pin.get("updated_time")
        ):
            sections.append(f"发布时间: {created_text}")
        return self._join_sections(sections)

    def _author_sections(self, author: Any, *, label: str) -> list[str]:
        sections: list[str] = []
        if author is None:
            return sections
        sections.append(f"{label}: {author.name}")
        return sections

    @staticmethod
    def _join_sections(sections: list[str]) -> str:
        return "\n\n".join(section for section in sections if section).strip()

    @staticmethod
    def _article_column_title(article: dict[str, Any]) -> str | None:
        column = article.get("column") or {}
        if not isinstance(column, dict):
            return None
        title = str(column.get("title") or "").strip()
        return title or None

    @staticmethod
    def _format_stats_line(stats: list[StatItem]) -> str:
        return " | ".join(f"{label} {value}" for label, value in stats if value)

    def _format_timestamp(self, value: Any) -> str | None:
        timestamp = self._safe_int(value)
        if timestamp is None or timestamp <= 0:
            return None
        if timestamp >= 10**12:
            timestamp //= 1000
        try:
            dt = datetime.fromtimestamp(timestamp, tz=self.cfg.timezone)
        except Exception:
            return None
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _format_count(self, value: Any) -> str:
        number = self._safe_int(value)
        if number is None:
            return self._normalize_text(str(value or ""))
        if abs(number) >= 100000000:
            text = f"{number / 100000000:.1f}".rstrip("0").rstrip(".")
            return f"{text}亿"
        if abs(number) >= 10000:
            text = f"{number / 10000:.1f}".rstrip("0").rstrip(".")
            return f"{text}万"
        return str(number)

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return int(stripped)
            except ValueError:
                try:
                    return int(float(stripped))
                except ValueError:
                    return None
        try:
            return int(value)
        except Exception:
            return None
