from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from ...data import MediaContent, SendGroup, TextContent, VideoContent
from .common import BodyBlock, VideoEntry


class ZhihuContentMixin:
    def _build_contents_and_groups(
        self,
        header_text: str,
        body_blocks: list[BodyBlock],
        video_entries: list[VideoEntry],
        *,
        request_headers: dict[str, str],
    ) -> tuple[list[MediaContent], list[SendGroup]]:
        video_contents = self._build_video_contents(
            video_entries,
            request_headers=request_headers,
        )

        contents: list[MediaContent] = []
        primary_contents: list[MediaContent] = []
        image_urls = [
            block["value"]
            for block in body_blocks
            if block.get("kind") == "image" and block.get("value")
        ]
        image_iter = iter(
            self.create_image_contents(image_urls, headers=request_headers)
        )
        pending_text_parts: list[str] = []

        def append_text_part(text: str) -> None:
            value = text.strip()
            if value:
                pending_text_parts.append(value)

        def flush_text_parts() -> None:
            if not pending_text_parts:
                return
            text_content = TextContent(self._join_sections(pending_text_parts))
            contents.append(text_content)
            primary_contents.append(text_content)
            pending_text_parts.clear()

        append_text_part(header_text)
        for block in body_blocks:
            kind = block.get("kind")
            value = str(block.get("value") or "").strip()
            if not value:
                continue
            if kind == "text":
                append_text_part(value)
                continue
            if kind != "image":
                continue
            flush_text_parts()
            image_content = next(image_iter, None)
            if image_content is None:
                continue
            contents.append(image_content)
            primary_contents.append(image_content)

        flush_text_parts()
        contents.extend(video_contents)

        send_groups: list[SendGroup] = []
        if primary_contents:
            send_groups.append(
                SendGroup(
                    contents=primary_contents,
                    force_merge=False,
                    render_card=True,
                )
            )
        for video in video_contents:
            send_groups.append(SendGroup(contents=[video], force_merge=False))
        return contents, send_groups

    @staticmethod
    def _pin_content_html(pin: dict[str, Any]) -> str:
        return str(pin.get("content_html") or pin.get("contentHtml") or "").strip()

    def _pin_plain_text(self, pin: dict[str, Any]) -> str:
        content = pin.get("content")
        if isinstance(content, str):
            return self._normalize_text(content, keep_newlines=True)
        if content is None:
            return ""
        if isinstance(content, (dict, list)):
            value = self._find_text_value(
                content,
                (
                    "content",
                    "text",
                    "description",
                    "title",
                ),
            )
            return self._normalize_text(value or "", keep_newlines=True)
        return self._normalize_text(str(content), keep_newlines=True)

    def _pin_timestamp(self, pin: dict[str, Any]) -> int | None:
        return self._safe_int(pin.get("created_time") or pin.get("updated_time"))

    async def _extract_content(
        self,
        html_text: str,
        initial_data: dict[str, Any],
        *,
        page_url: str,
        include_state_videos: bool = True,
    ) -> tuple[str, list[BodyBlock], list[VideoEntry]]:
        body_text = self._html_to_text(html_text, keep_newlines=True)
        body_blocks: list[BodyBlock] = []
        video_entries: list[VideoEntry] = []

        if html_text.strip():
            media_soup = BeautifulSoup(html_text, "html.parser")
            body_blocks = self._extract_ordered_body_blocks(
                media_soup, page_url=page_url
            )
            for node in media_soup.find_all(["video", "source", "iframe"]):
                self._append_video_entry(
                    video_entries,
                    self._extract_video_entry_from_tag(node, page_url),
                )

        if include_state_videos:
            video_entries = self._merge_unique_video_entries(
                video_entries,
                self._extract_video_entries_from_state(initial_data, page_url),
            )

        return body_text, body_blocks, video_entries

    def _build_section_blocks(
        self,
        title: str | None,
        body_blocks: list[BodyBlock],
        fallback_text: str,
    ) -> list[BodyBlock]:
        normalized_fallback = self._normalize_text(
            fallback_text,
            keep_newlines=True,
        )
        if not body_blocks and not normalized_fallback:
            return []

        section_blocks: list[BodyBlock] = []
        if title:
            section_blocks.append(self._make_text_block(title))
        if body_blocks:
            section_blocks.extend(body_blocks)
            return section_blocks

        section_blocks.append(self._make_text_block(normalized_fallback))
        return section_blocks

    def _extract_ordered_body_blocks(
        self,
        root: Tag | BeautifulSoup,
        *,
        page_url: str,
    ) -> list[BodyBlock]:
        body_blocks: list[BodyBlock] = []
        text_blocks: list[str] = []
        seen_images: set[str] = set()
        self._append_ordered_body_container(
            root,
            body_blocks,
            text_blocks,
            seen_images,
            page_url,
        )
        self._flush_body_text_blocks(body_blocks, text_blocks)
        return self._merge_adjacent_body_text_blocks(body_blocks)

    def _append_ordered_body_container(
        self,
        node: Tag | BeautifulSoup,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
        seen_images: set[str],
        page_url: str,
    ) -> None:
        for child in node.children:
            if isinstance(child, (Tag, NavigableString)):
                self._append_ordered_body_node(
                    child,
                    body_blocks,
                    text_blocks,
                    seen_images,
                    page_url,
                )

    def _append_ordered_body_node(
        self,
        node: Tag | NavigableString,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
        seen_images: set[str],
        page_url: str,
    ) -> None:
        if isinstance(node, NavigableString):
            text = self._normalize_text(str(node))
            if text:
                self._append_text_block(text_blocks, text)
            return

        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in {
            "script",
            "style",
            "noscript",
            "video",
            "source",
            "iframe",
            "audio",
            "svg",
        }:
            return

        if name == "img":
            self._flush_body_text_blocks(body_blocks, text_blocks)
            self._append_body_image_block(
                body_blocks,
                seen_images,
                self._extract_image_url(node, page_url),
            )
            return

        if name == "br":
            if text_blocks:
                text_blocks[-1] = text_blocks[-1].rstrip() + "\n"
            return

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "blockquote":
            self._append_text_block(
                text_blocks,
                self._format_blockquote_text(self._node_text(node, keep_newlines=True)),
            )
            return

        if name in {"ul", "ol"}:
            self._append_text_block(
                text_blocks,
                self._format_list_text(
                    self._collect_list_items(node),
                    ordered=name == "ol",
                ),
            )
            return

        if name == "li":
            item_text = self._node_text(node, keep_newlines=True)
            if item_text:
                self._append_text_block(
                    text_blocks,
                    self._format_list_text([item_text], ordered=False),
                )
            return

        if name == "pre":
            self._append_text_block(
                text_blocks,
                self._format_code_block(
                    self._extract_code_block(node),
                    self._extract_code_language(node),
                ),
            )
            return

        if name == "code":
            parent = node.parent
            if isinstance(parent, Tag) and (parent.name or "").lower() == "pre":
                return
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "hr":
            self._append_text_block(text_blocks, "---")
            return

        block_tags = {
            "p",
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "aside",
            "figure",
            "figcaption",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
        }
        has_block_child = any(
            isinstance(child, Tag)
            and (child.name or "").lower()
            in {
                "p",
                "div",
                "section",
                "article",
                "blockquote",
                "ul",
                "ol",
                "pre",
                "figure",
                "table",
                "hr",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            }
            for child in node.children
        )
        if name in {"figure", "figcaption"} or (
            name in block_tags and self._has_media_child(node)
        ):
            self._append_ordered_body_container(
                node,
                body_blocks,
                text_blocks,
                seen_images,
                page_url,
            )
            return

        if name in block_tags and has_block_child:
            self._append_ordered_body_container(
                node,
                body_blocks,
                text_blocks,
                seen_images,
                page_url,
            )
            return

        if name in block_tags:
            text = self._node_text(node, keep_newlines=True)
            if text:
                self._append_text_block(text_blocks, text)
            return

        text = self._node_text(node, keep_newlines=True)
        if text:
            self._append_text_block(text_blocks, text)
            return

        self._append_ordered_body_container(
            node,
            body_blocks,
            text_blocks,
            seen_images,
            page_url,
        )

    def _flush_body_text_blocks(
        self,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
    ) -> None:
        if not text_blocks:
            return
        text = self._compact_text_blocks(text_blocks)
        text_blocks.clear()
        if text:
            body_blocks.append(self._make_text_block(text))

    def _append_body_image_block(
        self,
        body_blocks: list[BodyBlock],
        seen_images: set[str],
        candidate: str | None,
    ) -> None:
        normalized = self._normalize_media_url(candidate)
        if not normalized or not self._looks_like_image_url(normalized):
            return
        key = self._media_key(normalized)
        if not key or key in seen_images:
            return
        seen_images.add(key)
        body_blocks.append(self._make_image_block(normalized))

    def _merge_adjacent_body_text_blocks(
        self,
        body_blocks: list[BodyBlock],
    ) -> list[BodyBlock]:
        merged: list[BodyBlock] = []
        pending_texts: list[str] = []

        def flush_texts() -> None:
            if not pending_texts:
                return
            merged.append(self._make_text_block(self._join_sections(pending_texts)))
            pending_texts.clear()

        for block in body_blocks:
            kind = block.get("kind")
            value = str(block.get("value") or "").strip()
            if not value:
                continue
            if kind == "text":
                pending_texts.append(value)
                continue
            flush_texts()
            if kind == "image":
                merged.append(block)

        flush_texts()
        return merged

    @staticmethod
    def _make_text_block(text: str) -> BodyBlock:
        return {"kind": "text", "value": text}

    @staticmethod
    def _make_image_block(url: str) -> BodyBlock:
        return {"kind": "image", "value": url}

    def _append_node_content(
        self,
        node: Tag | NavigableString,
        text_blocks: list[str],
    ) -> None:
        if isinstance(node, NavigableString):
            text = self._normalize_text(str(node))
            if text:
                self._append_text_block(text_blocks, text)
            return

        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in {
            "script",
            "style",
            "noscript",
            "img",
            "video",
            "source",
            "iframe",
            "audio",
            "svg",
        }:
            return

        if name == "br":
            if text_blocks:
                text_blocks[-1] = text_blocks[-1].rstrip() + "\n"
            return

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "blockquote":
            self._append_text_block(
                text_blocks,
                self._format_blockquote_text(self._node_text(node, keep_newlines=True)),
            )
            return

        if name in {"ul", "ol"}:
            self._append_text_block(
                text_blocks,
                self._format_list_text(
                    self._collect_list_items(node),
                    ordered=name == "ol",
                ),
            )
            return

        if name == "li":
            item_text = self._node_text(node, keep_newlines=True)
            if item_text:
                self._append_text_block(
                    text_blocks,
                    self._format_list_text([item_text], ordered=False),
                )
            return

        if name == "pre":
            self._append_text_block(
                text_blocks,
                self._format_code_block(
                    self._extract_code_block(node),
                    self._extract_code_language(node),
                ),
            )
            return

        if name == "code":
            parent = node.parent
            if isinstance(parent, Tag) and (parent.name or "").lower() == "pre":
                return
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "hr":
            self._append_text_block(text_blocks, "---")
            return

        block_tags = {
            "p",
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "aside",
            "figure",
            "figcaption",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
        }
        has_block_child = any(
            isinstance(child, Tag)
            and (child.name or "").lower()
            in {
                "p",
                "div",
                "section",
                "article",
                "blockquote",
                "ul",
                "ol",
                "pre",
                "figure",
                "table",
                "hr",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            }
            for child in node.children
        )
        if name in block_tags and has_block_child:
            self._append_container_content(node, text_blocks)
            return

        if name in block_tags:
            if self._has_media_child(node) and not self._node_text(
                node, keep_newlines=True
            ):
                return
            self._append_text_block(
                text_blocks,
                self._node_text(node, keep_newlines=True),
            )
            return

        text = self._node_text(node, keep_newlines=True)
        if text:
            self._append_text_block(text_blocks, text)
            return

        self._append_container_content(node, text_blocks)

    def _append_container_content(
        self,
        node: Tag | BeautifulSoup,
        text_blocks: list[str],
    ) -> None:
        for child in node.children:
            if isinstance(child, (Tag, NavigableString)):
                self._append_node_content(child, text_blocks)

    def _append_text_block(self, text_blocks: list[str], text: str) -> None:
        value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not value:
            return
        if value.startswith("```"):
            text_blocks.append(value)
            return
        text_blocks.append(self._normalize_text(value, keep_newlines=True))

    def _compact_text_blocks(self, text_blocks: list[str]) -> str:
        compacted: list[str] = []

        def is_special(block: str) -> bool:
            stripped = block.lstrip()
            return (
                not stripped
                or stripped.startswith("```")
                or stripped.startswith(">")
                or stripped.startswith("- ")
                or stripped == "---"
                or bool(re.match(r"^\d+\.\s", stripped))
                or "\n" in stripped
            )

        for block in text_blocks:
            value = block.strip()
            if not value:
                continue
            if compacted and not is_special(compacted[-1]) and not is_special(value):
                compacted[-1] = self._normalize_text(f"{compacted[-1]} {value}")
                continue
            compacted.append(value)
        return "\n\n".join(compacted).strip()

    def _format_blockquote_text(self, text: str) -> str:
        value = self._normalize_text(text, keep_newlines=True)
        if not value:
            return ""
        return "\n".join(
            f"> {line}" if line else ">" for line in value.splitlines()
        ).strip()

    def _format_list_text(self, items: list[str], *, ordered: bool) -> str:
        lines: list[str] = []
        for index, item in enumerate(items, start=1):
            value = self._normalize_text(item, keep_newlines=True)
            if not value:
                continue
            prefix = f"{index}. " if ordered else "- "
            parts = value.splitlines() or [value]
            lines.append(prefix + parts[0])
            indent = " " * len(prefix)
            for line in parts[1:]:
                lines.append(indent + line)
        return "\n".join(lines).strip()

    def _format_code_block(self, code: str, language: str | None = None) -> str:
        value = code.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not value.strip():
            return ""
        lang = re.sub(r"[^A-Za-z0-9_+#.-]+", "", language or "")
        if lang:
            return f"```{lang}\n{value}\n```"
        return f"```\n{value}\n```"

    def _has_media_child(self, node: Tag) -> bool:
        return (
            node.find(["img", "video", "source", "iframe"], recursive=True) is not None
        )

    def _collect_list_items(self, list_node: Tag) -> list[str]:
        items: list[str] = []
        for li in list_node.find_all("li", recursive=False):
            li_copy_soup = BeautifulSoup(str(li), "html.parser")
            li_copy = li_copy_soup.find("li")
            if li_copy is None:
                continue

            nested_blocks: list[str] = []
            for nested in li.find_all(["ul", "ol"], recursive=False):
                nested_text = self._format_list_text(
                    self._collect_list_items(nested),
                    ordered=(nested.name or "").lower() == "ol",
                )
                if nested_text:
                    nested_blocks.append(
                        "\n".join(f"  {line}" for line in nested_text.splitlines())
                    )

            for nested in li_copy.find_all(["ul", "ol"]):
                nested.decompose()

            base_text = self._node_text(li_copy, keep_newlines=True)
            combined = "\n".join(
                part for part in [base_text, *nested_blocks] if part
            ).strip()
            if combined:
                items.append(combined)
        return items

    def _extract_code_block(self, pre_tag: Tag) -> str:
        code_tag = pre_tag.find("code")
        source = code_tag if isinstance(code_tag, Tag) else pre_tag
        return html.unescape(source.get_text("", strip=False))

    def _extract_code_language(self, pre_tag: Tag) -> str | None:
        for tag in (pre_tag, pre_tag.find("code")):
            if not isinstance(tag, Tag):
                continue
            for key in ("data-language", "data-lang", "lang"):
                value = tag.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for item in self._iter_attr_strings(tag.get("class")):
                matched = re.search(
                    r"(?:lang|language)[-_]?([A-Za-z0-9_+#.-]+)",
                    item,
                    re.I,
                )
                if matched:
                    return matched.group(1)
        return None

    def _append_image_url(self, image_urls: list[str], candidate: str | None) -> None:
        normalized = self._normalize_media_url(candidate)
        if not normalized or not self._looks_like_image_url(normalized):
            return
        key = self._media_key(normalized)
        if not key:
            return
        if any(self._media_key(item) == key for item in image_urls):
            return
        image_urls.append(normalized)

    def _append_video_entry(
        self,
        video_entries: list[VideoEntry],
        candidate: VideoEntry | None,
    ) -> None:
        if not candidate:
            return

        url = self._normalize_media_url(candidate.get("url"))
        if not url or not self._looks_like_video_url(url):
            return

        entry: VideoEntry = {
            "url": url,
            "cover_url": self._normalize_media_url(candidate.get("cover_url")) or None,
            "title": self._normalize_text(str(candidate.get("title") or "")) or None,
        }
        key = self._media_key(url)
        if not key:
            return

        for existing in video_entries:
            if self._media_key(existing.get("url")) != key:
                continue
            if not existing.get("cover_url") and entry.get("cover_url"):
                existing["cover_url"] = entry["cover_url"]
            if not existing.get("title") and entry.get("title"):
                existing["title"] = entry["title"]
            return

        video_entries.append(entry)

    def _extract_image_url(self, tag: Tag, page_url: str) -> str | None:
        for key in (*self._MEDIA_ATTRS, "srcset"):
            if key not in tag.attrs:
                continue
            for raw in self._iter_attr_strings(tag.attrs.get(key)):
                candidates = re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw)
                if not candidates and raw:
                    candidates = [raw.split()[0]]
                for candidate in candidates:
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_image_url(normalized):
                        return normalized

        for raw in self._iter_attr_strings(tag.attrs):
            for candidate in re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw):
                normalized = self._normalize_media_url(candidate, page_url)
                if normalized and self._looks_like_image_url(normalized):
                    return normalized
        return None

    def _extract_video_entry_from_tag(
        self, tag: Tag, page_url: str
    ) -> VideoEntry | None:
        url: str | None = None

        for key in (*self._VIDEO_URL_KEYS, *self._MEDIA_ATTRS):
            if key not in tag.attrs:
                continue
            for raw in self._iter_attr_strings(tag.attrs.get(key)):
                candidates = re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw)
                if not candidates and raw:
                    candidates = [raw]
                for candidate in candidates:
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_video_url(normalized):
                        url = normalized
                        break
                if url:
                    break
            if url:
                break

        if not url:
            for raw in self._iter_attr_strings(tag.attrs):
                for candidate in re.findall(
                    r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+",
                    raw,
                ):
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_video_url(normalized):
                        url = normalized
                        break
                if url:
                    break

        if not url:
            return None

        title = (
            self._normalize_text(
                str(tag.get("title") or tag.get("aria-label") or tag.get("alt") or "")
            )
            or None
        )
        return {
            "url": url,
            "cover_url": self._extract_cover_url(tag, page_url),
            "title": title,
        }

    def _extract_cover_url(self, tag: Tag, page_url: str) -> str | None:
        for node in (tag, tag.parent if isinstance(tag.parent, Tag) else None):
            if not isinstance(node, Tag):
                continue
            for key in (*self._VIDEO_COVER_KEYS, *self._MEDIA_ATTRS):
                if key not in node.attrs:
                    continue
                for raw in self._iter_attr_strings(node.attrs.get(key)):
                    normalized = self._normalize_media_url(raw, page_url)
                    if normalized and self._looks_like_image_url(normalized):
                        return normalized
        return None

    def _build_video_contents(
        self,
        video_entries: list[VideoEntry],
        *,
        request_headers: dict[str, str],
    ) -> list[VideoContent]:
        contents: list[VideoContent] = []
        for entry in video_entries:
            video_url = str(entry.get("url") or "").strip()
            if not video_url:
                continue
            contents.append(
                self._build_video_content_from_url(
                    video_url,
                    cover_url=entry.get("cover_url"),
                    request_headers=request_headers,
                )
            )
        return contents

    def _build_video_content_from_url(
        self,
        video_url: str,
        *,
        cover_url: str | None = None,
        request_headers: dict[str, str],
    ) -> VideoContent:
        if ".m3u8" in video_url.lower():
            task = self.downloader.ytdlp_download_video_relaxed(
                video_url,
                headers=request_headers,
                proxy=self.proxy,
            )
            return self.create_video_content_by_task(
                task,
                cover_url,
                headers=request_headers,
            )
        return self.create_video_content(
            video_url,
            cover_url,
            headers=request_headers,
        )

    def _extract_video_entries_from_state(
        self,
        initial_data: dict[str, Any],
        page_url: str,
    ) -> list[VideoEntry]:
        entries: list[VideoEntry] = []
        queue: list[Any] = [initial_data.get("initialState") or initial_data]
        seen: set[int] = set()

        while queue:
            value = queue.pop()
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                self._append_video_entry(
                    entries,
                    self._extract_video_entry_from_mapping(value, page_url),
                )
                queue.extend(value.values())
                continue

            if isinstance(value, list):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                queue.extend(value)

        return entries

    def _extract_video_entry_from_mapping(
        self,
        mapping: dict[str, Any],
        page_url: str,
    ) -> VideoEntry | None:
        lowered_keys = {str(key).lower() for key in mapping.keys()}
        if not lowered_keys:
            return None

        has_video_signal = any(
            token in key
            for key in lowered_keys
            for token in ("video", "play", "stream", "playlist", "cover", "poster")
        )
        if not has_video_signal:
            return None

        video_url = self._find_media_value(
            mapping,
            self._looks_like_video_url,
            self._VIDEO_URL_KEYS,
        )
        if not video_url:
            return None

        return {
            "url": self._normalize_media_url(video_url, page_url),
            "cover_url": self._normalize_media_url(
                self._find_media_value(
                    mapping,
                    self._looks_like_image_url,
                    self._VIDEO_COVER_KEYS,
                ),
                page_url,
            )
            or None,
            "title": self._find_text_value(mapping, self._VIDEO_TITLE_KEYS),
        }

    def _find_media_value(
        self,
        value: Any,
        predicate: Any,
        preferred_keys: tuple[str, ...] = (),
    ) -> str | None:
        seen: set[int] = set()
        preferred = {key.lower() for key in preferred_keys}

        def visit(current: Any) -> str | None:
            if isinstance(current, str):
                normalized = self._normalize_state_media_url(current)
                if normalized and predicate(normalized):
                    return normalized
                return None

            if isinstance(current, dict):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)

                for key in preferred_keys:
                    for current_key, nested in current.items():
                        if str(current_key).lower() == key.lower():
                            found = visit(nested)
                            if found:
                                return found

                for current_key, nested in current.items():
                    if str(current_key).lower() in preferred:
                        continue
                    found = visit(nested)
                    if found:
                        return found
                return None

            if isinstance(current, list):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)
                for nested in current:
                    found = visit(nested)
                    if found:
                        return found
            return None

        return visit(value)

    def _find_text_value(
        self,
        value: Any,
        preferred_keys: tuple[str, ...] = (),
    ) -> str | None:
        seen: set[int] = set()
        preferred = {key.lower() for key in preferred_keys}

        def pick_text(raw: str) -> str | None:
            text = self._normalize_text(raw)
            if not text:
                return None
            lowered = text.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                return None
            return text

        def visit(current: Any) -> str | None:
            if isinstance(current, str):
                return pick_text(current)

            if isinstance(current, dict):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)

                for key in preferred_keys:
                    for current_key, nested in current.items():
                        if str(current_key).lower() == key.lower():
                            found = visit(nested)
                            if found:
                                return found

                for current_key, nested in current.items():
                    if str(current_key).lower() in preferred:
                        continue
                    found = visit(nested)
                    if found:
                        return found
                return None

            if isinstance(current, list):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)
                for nested in current:
                    found = visit(nested)
                    if found:
                        return found
            return None

        return visit(value)

    def _iter_attr_strings(self, value: Any):
        if isinstance(value, str):
            yield value
            return

        if isinstance(value, dict):
            for item in value.values():
                yield from self._iter_attr_strings(item)
            return

        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from self._iter_attr_strings(item)

    def _node_text(
        self,
        node: Tag | NavigableString,
        *,
        keep_newlines: bool = False,
    ) -> str:
        if isinstance(node, NavigableString):
            return self._normalize_text(str(node), keep_newlines=keep_newlines)
        if not isinstance(node, Tag):
            return ""
        separator = "\n" if keep_newlines else " "
        return self._normalize_text(
            node.get_text(separator=separator, strip=False),
            keep_newlines=keep_newlines,
        )

    def _html_to_text(self, html_text: str, *, keep_newlines: bool = False) -> str:
        if not html_text.strip():
            return ""

        soup = BeautifulSoup(html_text, "html.parser")
        for node in soup.find_all(
            [
                "script",
                "style",
                "noscript",
                "img",
                "video",
                "source",
                "iframe",
                "audio",
                "svg",
            ]
        ):
            node.decompose()

        text_blocks: list[str] = []
        self._append_container_content(soup, text_blocks)
        text = self._compact_text_blocks(text_blocks)
        if keep_newlines:
            return text
        return self._normalize_text(text)

    def _normalize_text(self, text: str, *, keep_newlines: bool = False) -> str:
        if not text:
            return ""
        value = html.unescape(text)
        value = value.replace("\xa0", " ").replace("\u3000", " ")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t\f\v]+", " ", value)
        if keep_newlines:
            value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
            value = re.sub(r"\n{3,}", "\n\n", value)
        else:
            value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _normalize_media_url(
        self,
        url: str | None,
        page_url: str | None = None,
    ) -> str:
        if not url:
            return ""
        value = html.unescape(str(url)).strip().strip("\"'")
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        value = value.replace("&amp;", "&")
        value = value.rstrip(".,);")
        if not value or value.startswith(("data:", "blob:")):
            return ""
        if value.startswith("//"):
            value = "https:" + value
        elif page_url and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
            value = urljoin(page_url, value)
        if not value.startswith(("http://", "https://")):
            return ""
        return value

    def _normalize_state_media_url(self, url: str | None) -> str:
        if not url:
            return ""
        value = html.unescape(str(url)).strip().strip("\"'")
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        matched = re.search(r"https?://[^\s'\"<>]+|//[^\s'\"<>]+", value)
        if matched:
            value = matched.group(0)
        return self._normalize_media_url(value)

    def _looks_like_video_url(self, url: str | None) -> bool:
        if not url:
            return False
        value = url.lower()
        if not value.startswith(("http://", "https://")):
            return False
        if self._looks_like_image_url(value):
            return False
        if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", value):
            return True
        return any(
            marker in value
            for marker in (
                "video.zhihu.com",
                "/playlist.m3u8",
                "/playback/",
                "/stream/",
                ".vod.",
            )
        )

    def _looks_like_image_url(self, url: str | None) -> bool:
        if not url:
            return False
        value = url.lower()
        if not value.startswith(("http://", "https://")):
            return False
        if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", value):
            return False
        if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|avif)(?:$|[?#])", value):
            return True
        return any(
            host in value
            for host in (
                "picx.zhimg.com",
                "pic1.zhimg.com",
                "pic2.zhimg.com",
                "zhimg.com",
            )
        )

    def _media_key(self, url: str | None) -> str:
        normalized = self._normalize_media_url(url)
        if not normalized:
            return ""
        value = normalized.split("#", 1)[0]
        if self._looks_like_image_url(value) or self._looks_like_video_url(value):
            value = value.split("?", 1)[0]
        return value.replace("http://", "https://")

    def _merge_unique_urls(self, *groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for url in group:
                key = self._media_key(url)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(url)
        return merged

    def _merge_unique_video_entries(
        self,
        *groups: list[VideoEntry],
    ) -> list[VideoEntry]:
        merged: dict[str, VideoEntry] = {}
        order: list[str] = []

        for group in groups:
            for entry in group:
                url = self._normalize_media_url(entry.get("url"))
                key = self._media_key(url)
                if not key:
                    continue
                normalized_entry: VideoEntry = {
                    "url": url,
                    "cover_url": self._normalize_media_url(entry.get("cover_url"))
                    or None,
                    "title": self._normalize_text(str(entry.get("title") or ""))
                    or None,
                }
                if key not in merged:
                    merged[key] = normalized_entry
                    order.append(key)
                    continue
                if not merged[key].get("cover_url") and normalized_entry.get(
                    "cover_url"
                ):
                    merged[key]["cover_url"] = normalized_entry["cover_url"]
                if not merged[key].get("title") and normalized_entry.get("title"):
                    merged[key]["title"] = normalized_entry["title"]

        return [merged[key] for key in order]
