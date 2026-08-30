"""B站视频专属预览卡片

参考 B站分享卡片风格渲染：渐变圆角背景、平台徽章、圆角封面、
播放数据栏、互动数据面板、简介面板、底部 BV 号与水印。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from apilmoji import Apilmoji
from apilmoji.core import get_font_height
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .data import ParseResult
from .render import FontInfo, Renderer, wrap_text
from .utils import format_count

Color = tuple[int, int, int]
RGBAColor = tuple[int, int, int, int]
PILImage = Image.Image


@lru_cache(maxsize=16)
def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    """加载并缓存字体"""
    return ImageFont.truetype(font_path, size)


@dataclass(frozen=True)
class BiliFontSet:
    """B站卡片字体集"""

    _FONT_SIZES = (
        ("badge", 22),
        ("meta", 22),
        ("title", 34),
        ("name", 26),
        ("date", 20),
        ("stat_num", 30),
        ("stat_label", 20),
        ("desc", 24),
        ("footer", 20),
    )
    """字体大小"""

    badge_font: FontInfo
    meta_font: FontInfo
    title_font: FontInfo
    name_font: FontInfo
    date_font: FontInfo
    stat_num_font: FontInfo
    stat_label_font: FontInfo
    desc_font: FontInfo
    footer_font: FontInfo

    @classmethod
    def new(cls, font_path: Path):
        font_infos: dict[str, FontInfo] = {}
        for name, size in cls._FONT_SIZES:
            font = _load_font(str(font_path), size)
            font_infos[f"{name}_font"] = FontInfo(
                font=font,
                line_height=get_font_height(font),
                cjk_width=size,
            )
        return cls(**font_infos)


class BiliVideoCardRenderer:
    """B站视频预览卡片渲染器"""

    # 布局配置
    CARD_WIDTH = 696
    """卡片主体宽度"""
    SHADOW_MARGIN = 24
    """阴影边距"""
    PAD = 24
    """内边距"""
    CARD_RADIUS = 24
    """卡片圆角"""
    COVER_RADIUS = 16
    """封面圆角"""
    PANEL_RADIUS = 16
    """面板圆角"""
    BADGE_HEIGHT = 40
    """平台徽章高度"""
    AVATAR_SIZE = 52
    """头像大小"""
    AVATAR_RING = 3
    """头像粉色描边宽度"""

    # 颜色配置
    PINK: Color = (251, 114, 153)
    """B站粉"""
    CYAN: Color = (0, 161, 214)
    """B站蓝（徽章图标）"""
    TITLE_COLOR: Color = (45, 47, 54)
    """标题色"""
    DARK: Color = (60, 62, 70)
    """深色文本"""
    GRAY: Color = (112, 115, 124)
    """灰色文本"""
    LIGHT_GRAY: Color = (150, 153, 162)
    """浅灰文本"""
    WHITE: Color = (255, 255, 255)
    """白色"""

    GRAD_TL: Color = (179, 222, 240)
    """渐变左上（蓝）"""
    GRAD_MID: Color = (236, 237, 242)
    """渐变中间"""
    GRAD_BR: Color = (238, 213, 224)
    """渐变右下（粉）"""

    PANEL_FILL: RGBAColor = (255, 255, 255, 150)
    """半透明白面板"""
    BADGE_FILL: RGBAColor = (30, 45, 55, 120)
    """半透明黑徽章"""
    SEP_COLOR: RGBAColor = (205, 208, 216, 255)
    """底部分隔线"""

    WATERMARK = "万能解析器"
    """右下角水印"""

    _FONTSET: ClassVar[BiliFontSet | None] = None
    """字体集缓存"""

    def __init__(self, renderer: Renderer):
        self.renderer = renderer
        if BiliVideoCardRenderer._FONTSET is None:
            BiliVideoCardRenderer._FONTSET = BiliFontSet.new(Renderer.DEFAULT_FONT_PATH)
        self.fontset = BiliVideoCardRenderer._FONTSET

    async def render(self, result: ParseResult) -> PILImage:
        """渲染卡片，返回带阴影的透明背景 PNG 图像"""
        card = result.extra.get("card") or {}
        stats: dict = card.get("stats") or {}
        content_w = self.CARD_WIDTH - 2 * self.PAD

        cover = self.renderer._load_and_resize_cover(
            await result.cover_path,
            content_width=content_w,
        )
        avatar = None
        if result.author:
            avatar = self.renderer._load_and_process_avatar(
                await result.author.get_avatar_path(),
                size=self.AVATAR_SIZE,
            )

        # 在透明图层上绘制全部内容，最后裁剪并合成渐变背景
        overlay = Image.new("RGBA", (self.CARD_WIDTH, 4000), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        y = self.PAD

        # 1. 平台徽章
        y = self._draw_badge(draw, y) + 20

        # 2. 封面
        if cover is not None:
            self._paste_rounded(overlay, cover, (self.PAD, y), self.COVER_RADIUS)
            y += cover.height + 28

        # 3. 数据栏
        y = await self._draw_meta(overlay, draw, y, card, stats) + 16

        # 4. 标题
        if result.title:
            lines = wrap_text(
                f"“{result.title}”",
                content_w,
                self.fontset.title_font,
            )
            y += (
                await self._text(
                    overlay, (self.PAD, y), lines, self.fontset.title_font, self.TITLE_COLOR
                )
                + 18
            )

        # 5. 作者
        y = self._draw_author(draw, overlay, y, result, avatar) + 20

        # 6. 互动数据面板
        y = self._draw_stats_panel(draw, y, stats, content_w) + 16

        # 7. 简介面板
        desc_parts = [p for p in (card.get("desc"), result.extra_info) if p]
        if desc_parts:
            y = self._draw_desc_panel(overlay, draw, y, "\n\n".join(desc_parts), content_w) + 20

        # 8. 底部 BV 号与水印
        y = self._draw_footer(draw, y, card.get("bvid"))

        # 裁剪内容图层
        card_h = y
        overlay = overlay.crop((0, 0, self.CARD_WIDTH, card_h))

        # 合成渐变圆角背景
        card_img = Image.new("RGBA", (self.CARD_WIDTH, card_h), (0, 0, 0, 0))
        mask = Image.new("L", (self.CARD_WIDTH, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.CARD_WIDTH - 1, card_h - 1),
            radius=self.CARD_RADIUS,
            fill=255,
        )
        card_img.paste(self._gradient(self.CARD_WIDTH, card_h), (0, 0), mask)
        card_img = Image.alpha_composite(card_img, overlay)

        # 添加阴影边距
        m = self.SHADOW_MARGIN
        final = Image.new(
            "RGBA",
            (self.CARD_WIDTH + 2 * m, card_h + 2 * m),
            (0, 0, 0, 0),
        )
        shadow = Image.new("RGBA", final.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (m, m + 6, m + self.CARD_WIDTH, m + card_h + 10),
            radius=self.CARD_RADIUS,
            fill=(70, 80, 100, 110),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(12))
        final = Image.alpha_composite(final, shadow)
        final.paste(card_img, (m, m), card_img)
        return final

    # ================ 绘制各部分 ================

    def _draw_badge(self, draw: ImageDraw.ImageDraw, y: int) -> int:
        """绘制平台徽章，返回底部 y"""
        text = "哔哩哔哩"
        font = self.fontset.badge_font
        icon_s = 26
        text_w = font.get_text_width(text)
        pad_x = 16
        gap = 10
        badge_w = pad_x + icon_s + gap + text_w + pad_x
        x = self.PAD

        draw.rounded_rectangle(
            (x, y, x + badge_w, y + self.BADGE_HEIGHT),
            radius=self.BADGE_HEIGHT // 2,
            fill=self.BADGE_FILL,
        )
        icon_y = y + (self.BADGE_HEIGHT - icon_s) // 2
        self._icon_tv(draw, x + pad_x, icon_y, icon_s, self.CYAN, eyes=True)
        draw.text(
            (x + pad_x + icon_s + gap, y + (self.BADGE_HEIGHT - font.line_height) // 2),
            text,
            font=font.font,
            fill=self.WHITE,
        )
        return y + self.BADGE_HEIGHT

    async def _draw_meta(
        self,
        overlay: PILImage,
        draw: ImageDraw.ImageDraw,
        y: int,
        card: dict,
        stats: dict,
    ) -> int:
        """绘制播放数据栏，返回底部 y"""
        font = self.fontset.meta_font
        icon_s = 24

        segments: list[tuple[str, str]] = []
        if stats.get("view"):
            segments.append(("play", format_count(stats["view"])))
        if stats.get("danmaku"):
            segments.append(("danmaku", format_count(stats["danmaku"])))
        if card.get("duration"):
            segments.append(("", f"时长 {self._fmt_duration(card['duration'])}"))
        if card.get("online"):
            segments.append(("", f"在线 {card['online']} 人在看"))

        x = self.PAD
        icon_y = y + (font.line_height - icon_s) // 2
        for i, (icon, text) in enumerate(segments):
            if i:
                # 分隔点
                cx = x + 14
                cy = y + font.line_height // 2
                draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=self.LIGHT_GRAY)
                x += 28
            if icon == "play":
                self._icon_tv(draw, x, icon_y, icon_s, self.GRAY)
                x += icon_s + 6
            elif icon == "danmaku":
                self._icon_tv(draw, x, icon_y, icon_s, self.GRAY, danmaku=True)
                x += icon_s + 6
            draw.text((x, y), text, font=font.font, fill=self.GRAY)
            x += font.get_text_width(text)
        return y + font.line_height

    def _draw_author(
        self,
        draw: ImageDraw.ImageDraw,
        overlay: PILImage,
        y: int,
        result: ParseResult,
        avatar: PILImage | None,
    ) -> int:
        """绘制作者行，返回底部 y"""
        size = self.AVATAR_SIZE
        ring = self.AVATAR_RING
        outer = size + 2 * ring

        # 粉色描边 + 头像
        draw.ellipse(
            (self.PAD, y, self.PAD + outer, y + outer),
            fill=self.PINK,
        )
        if avatar is not None:
            overlay.paste(avatar, (self.PAD + ring, y + ring), avatar)
        else:
            draw.ellipse(
                (self.PAD + ring, y + ring, self.PAD + ring + size, y + ring + size),
                fill=self.LIGHT_GRAY,
            )

        tx = self.PAD + outer + 14
        name_y = y + 2
        if result.author:
            draw.text(
                (tx, name_y),
                result.author.name,
                font=self.fontset.name_font.font,
                fill=self.DARK,
            )
        if result.timestamp is not None:
            date_text = result.formatted_datetime("%Y-%m-%d %H:%M") or ""
            draw.text(
                (tx, name_y + self.fontset.name_font.line_height + 2),
                date_text,
                font=self.fontset.date_font.font,
                fill=self.GRAY,
            )
        return y + outer

    def _draw_stats_panel(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        stats: dict,
        content_w: int,
    ) -> int:
        """绘制互动数据面板，返回底部 y"""
        f_num = self.fontset.stat_num_font
        f_label = self.fontset.stat_label_font
        icon_s = 24
        gap = 8
        top = 16
        h = top + f_num.line_height + 8 + f_label.line_height + 14

        draw.rounded_rectangle(
            (self.PAD, y, self.CARD_WIDTH - self.PAD, y + h),
            radius=self.PANEL_RADIUS,
            fill=self.PANEL_FILL,
        )

        items = (
            ("like", "点赞", self._icon_like),
            ("coin", "投币", self._icon_coin),
            ("favorite", "收藏", self._icon_star),
            ("share", "转发", self._icon_share),
            ("reply", "评论", self._icon_comment),
        )
        col_w = content_w / len(items)
        for i, (key, label, icon_fn) in enumerate(items):
            cx = self.PAD + col_w * i + col_w / 2
            num = format_count(stats.get(key, 0))
            num_w = f_num.get_text_width(num)
            sx = cx - (icon_s + gap + num_w) / 2
            icon_fn(draw, sx, y + top + (f_num.line_height - icon_s) // 2, icon_s, self.PINK)
            draw.text(
                (sx + icon_s + gap, y + top),
                num,
                font=f_num.font,
                fill=self.DARK,
            )
            label_w = f_label.get_text_width(label)
            draw.text(
                (cx - label_w / 2, y + top + f_num.line_height + 8),
                label,
                font=f_label.font,
                fill=self.GRAY,
            )
        return y + h

    def _draw_desc_panel(
        self,
        overlay: PILImage,
        draw: ImageDraw.ImageDraw,
        y: int,
        text: str,
        content_w: int,
    ) -> int:
        """绘制简介面板，返回底部 y"""
        f_desc = self.fontset.desc_font
        pad_x = 24
        pad_y = 20
        line_gap = 10
        lines = wrap_text(text, content_w - 2 * pad_x - 14, f_desc)
        h = 2 * pad_y + len(lines) * f_desc.line_height + (len(lines) - 1) * line_gap

        draw.rounded_rectangle(
            (self.PAD, y, self.CARD_WIDTH - self.PAD, y + h),
            radius=self.PANEL_RADIUS,
            fill=self.PANEL_FILL,
        )
        # 左侧粉色强调条
        draw.rounded_rectangle(
            (self.PAD + 14, y + 16, self.PAD + 19, y + h - 16),
            radius=2,
            fill=self.PINK,
        )
        # 逐行绘制（支持 emoji）
        ty = y + pad_y
        for line in lines:
            ty += self._text_sync(overlay, (self.PAD + pad_x + 14, ty), [line], f_desc, self.DARK)
            ty += line_gap
        return y + h

    def _draw_footer(self, draw: ImageDraw.ImageDraw, y: int, bvid: str | None) -> int:
        """绘制底部分隔线、BV 号与水印，返回底部 y"""
        draw.line((0, y, self.CARD_WIDTH, y), fill=self.SEP_COLOR, width=1)
        y += 16
        f_footer = self.fontset.footer_font
        if bvid:
            draw.text((self.PAD, y), bvid, font=f_footer.font, fill=self.GRAY)

        wm = self.WATERMARK
        wm_w = f_footer.get_text_width(wm)
        tx = self.CARD_WIDTH - self.PAD - wm_w
        draw.text((tx, y), wm, font=f_footer.font, fill=self.PINK)
        cy = y + f_footer.line_height // 2
        draw.ellipse((tx - 16, cy - 4, tx - 8, cy + 4), fill=self.PINK)
        return y + f_footer.line_height + self.PAD

    # ================ 工具方法 ================

    async def _text(
        self,
        image: PILImage,
        xy: tuple[int, int],
        lines: list[str],
        font: FontInfo,
        fill: Color,
    ) -> int:
        """绘制文本（支持 emoji），返回占用高度"""
        await Apilmoji.text(
            image,
            xy,
            lines,
            font.font,
            fill=fill,
            line_height=font.line_height,
            source=self.renderer.EMOJI_SOURCE,
        )
        return font.line_height * len(lines)

    def _text_sync(
        self,
        image: PILImage,
        xy: tuple[int, int],
        lines: list[str],
        font: FontInfo,
        fill: Color,
    ) -> int:
        """同步绘制纯文本（不含 emoji 的场景）"""
        ImageDraw.Draw(image).text(xy, lines[0], font=font.font, fill=fill)
        return font.line_height

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        """格式化时长为 m:ss / h:mm:ss"""
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @staticmethod
    def _gradient(w: int, h: int) -> PILImage:
        """生成对角渐变背景（左上蓝 → 右下粉）"""
        corners = Image.new("RGB", (2, 2))
        corners.putpixel((0, 0), BiliVideoCardRenderer.GRAD_TL)
        corners.putpixel((1, 0), BiliVideoCardRenderer.GRAD_MID)
        corners.putpixel((0, 1), BiliVideoCardRenderer.GRAD_MID)
        corners.putpixel((1, 1), BiliVideoCardRenderer.GRAD_BR)
        return corners.resize((w, h), Image.Resampling.BILINEAR)

    @staticmethod
    def _paste_rounded(
        canvas: PILImage,
        img: PILImage,
        xy: tuple[int, int],
        radius: int,
    ) -> None:
        """以圆角遮罩粘贴图片"""
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.width - 1, img.height - 1),
            radius=radius,
            fill=255,
        )
        canvas.paste(img, xy, mask)

    # ================ 矢量图标 ================

    @staticmethod
    def _icon_tv(
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        s: int,
        color: Color,
        danmaku: bool = False,
        eyes: bool = False,
    ) -> None:
        """B站小电视图标（轮廓）"""
        w = max(2, s // 10)
        # 天线
        draw.line(
            (x + 0.32 * s, y + 0.08 * s, x + 0.44 * s, y + 0.24 * s),
            fill=color,
            width=w,
        )
        draw.line(
            (x + 0.68 * s, y + 0.08 * s, x + 0.56 * s, y + 0.24 * s),
            fill=color,
            width=w,
        )
        # 机身
        draw.rounded_rectangle(
            (x, y + 0.24 * s, x + s, y + s),
            radius=int(0.2 * s),
            outline=color,
            width=w,
        )
        if danmaku:
            draw.line(
                (x + 0.26 * s, y + 0.50 * s, x + 0.74 * s, y + 0.50 * s),
                fill=color,
                width=w,
            )
            draw.line(
                (x + 0.26 * s, y + 0.72 * s, x + 0.58 * s, y + 0.72 * s),
                fill=color,
                width=w,
            )
        elif eyes:
            draw.ellipse(
                (x + 0.28 * s, y + 0.50 * s, x + 0.40 * s, y + 0.74 * s),
                fill=color,
            )
            draw.ellipse(
                (x + 0.60 * s, y + 0.50 * s, x + 0.72 * s, y + 0.74 * s),
                fill=color,
            )
        else:
            draw.polygon(
                [
                    (x + 0.40 * s, y + 0.45 * s),
                    (x + 0.40 * s, y + 0.79 * s),
                    (x + 0.70 * s, y + 0.62 * s),
                ],
                fill=color,
            )

    @staticmethod
    def _icon_like(
        draw: ImageDraw.ImageDraw, x: int, y: int, s: int, color: Color
    ) -> None:
        """点赞图标"""
        draw.rounded_rectangle(
            (x, y + 0.42 * s, x + 0.26 * s, y + 0.96 * s),
            radius=int(0.06 * s),
            fill=color,
        )
        draw.polygon(
            [
                (x + 0.32 * s, y + 0.96 * s),
                (x + 0.32 * s, y + 0.45 * s),
                (x + 0.50 * s, y + 0.40 * s),
                (x + 0.60 * s, y + 0.10 * s),
                (x + 0.75 * s, y + 0.10 * s),
                (x + 0.73 * s, y + 0.40 * s),
                (x + 0.93 * s, y + 0.40 * s),
                (x + 0.98 * s, y + 0.52 * s),
                (x + 0.90 * s, y + 0.96 * s),
            ],
            fill=color,
        )

    def _icon_coin(
        self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int, color: Color
    ) -> None:
        """投币图标"""
        draw.ellipse((x, y, x + s, y + s), fill=color)
        font = _load_font(str(Renderer.DEFAULT_FONT_PATH), int(s * 0.72))
        draw.text(
            (x + s / 2, y + s / 2),
            "币",
            font=font,
            fill=self.WHITE,
            anchor="mm",
        )

    @staticmethod
    def _icon_star(
        draw: ImageDraw.ImageDraw, x: int, y: int, s: int, color: Color
    ) -> None:
        """收藏图标（五角星）"""
        cx, cy = x + s / 2, y + s / 2
        r_out, r_in = s / 2, s * 0.21
        points = []
        for i in range(10):
            r = r_out if i % 2 == 0 else r_in
            angle = math.radians(-90 + i * 36)
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=color)

    @staticmethod
    def _icon_share(
        draw: ImageDraw.ImageDraw, x: int, y: int, s: int, color: Color
    ) -> None:
        """转发图标"""
        w = max(2, int(s * 0.16))
        draw.line(
            [
                (x + 0.12 * s, y + 0.92 * s),
                (x + 0.12 * s, y + 0.60 * s),
                (x + 0.40 * s, y + 0.32 * s),
            ],
            fill=color,
            width=w,
            joint="curve",
        )
        draw.polygon(
            [
                (x + 0.38 * s, y + 0.10 * s),
                (x + 0.95 * s, y + 0.36 * s),
                (x + 0.38 * s, y + 0.62 * s),
            ],
            fill=color,
        )

    @staticmethod
    def _icon_comment(
        draw: ImageDraw.ImageDraw, x: int, y: int, s: int, color: Color
    ) -> None:
        """评论图标"""
        draw.rounded_rectangle(
            (x, y + 0.06 * s, x + s, y + 0.76 * s),
            radius=int(0.16 * s),
            fill=color,
        )
        draw.polygon(
            [
                (x + 0.18 * s, y + 0.70 * s),
                (x + 0.48 * s, y + 0.70 * s),
                (x + 0.18 * s, y + 0.98 * s),
            ],
            fill=color,
        )
