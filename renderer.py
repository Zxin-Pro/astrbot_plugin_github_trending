"""GitHub Trending 排行榜图片渲染器。

使用 Pillow 生成深色 GitHub 风格排行榜图片：
- 2x 缩放渲染（1600px 宽），高 DPI 屏幕（手机/Retina）上更清晰
- 所有图标（五角星/上升三角/语言圆点）用 Pillow 原生绘制，零 emoji 字体依赖
- 中英文混排使用 CJK + 拉丁双字体回退，兼容 Linux 无中文字体环境
"""
from __future__ import annotations

import io
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from PIL import Image, ImageDraw, ImageFont

try:
    from .fetcher import RepoInfo
except ImportError:  # 直接以脚本方式测试时
    from fetcher import RepoInfo

# ── t2i 直连渲染（烛之播报模板，与积分游戏钓鱼播报同款）────────────────────

T2I_DIRECT_ENDPOINTS = ["https://t2i.soulter.top/text2img"]

try:
    from .t2i_template import TRENDING_T2I_TEMPLATE
except ImportError:
    from t2i_template import TRENDING_T2I_TEMPLATE


async def render_trending_t2i(repos: list, language: str = "", date_str: str = "") -> bytes:
    """用烛之播报 t2i 模板渲染榜单，返回图片字节（jpeg）。

    依次尝试 trust_env=False（直连）与 trust_env=True（走系统代理）。
    """
    import html as _html
    import aiohttp

    def esc(s: str) -> str:
        return _html.escape(str(s), quote=False)

    tmpl_data = {
        "title": "GitHub Trending 实况" + (f" · {language}" if language else ""),
        "date": date_str,
        "repos": [
            {
                "rank": r.rank,
                "name": esc(r.full_name),
                "count": esc(f"+{r.stars_today} today · ⭐ {r.stars_str}"),
                "events": [e for e in [
                    esc(r.description) if r.description else "",
                    esc(f"· 语言 {r.language}") if r.language else "",
                    esc(r.url),
                ] if e],
            }
            for r in repos
        ],
    }
    post = {
        "tmpl": TRENDING_T2I_TEMPLATE, "json": True, "tmpldata": tmpl_data,
        "options": {"full_page": True, "type": "jpeg", "quality": 70},
    }
    last_exc = None
    for ep in T2I_DIRECT_ENDPOINTS:
        for trust_env in (False, True):
            try:
                async with aiohttp.ClientSession(trust_env=trust_env) as session:
                    async with session.post(
                        f"{ep}/generate", json=post,
                        headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        ret = await resp.json()
                    img_url = f"{ep}/{ret['data']['id']}"
                    async with session.get(
                        img_url, headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as img_resp:
                        if img_resp.status != 200:
                            raise RuntimeError(f"HTTP {img_resp.status}")
                        raw = await img_resp.read()
                if raw:
                    return raw
                raise RuntimeError("t2i 返回空图片")
            except Exception as e:
                last_exc = e
    raise last_exc or RuntimeError("t2i 直连渲染失败")

# ── 渲染倍率 ──────────────────────────────────────────────────────────────
SCALE = 2  # 2x 渲染：1600px 宽

# ── 配色 (GitHub Dark 风格) ──────────────────────────────────────────────
BG_COLOR = "#0d1117"
CARD_BG = "#161b22"
TEXT_PRIMARY = "#e6edf3"
TEXT_SECONDARY = "#8b949e"
TEXT_TERTIARY = "#6e7681"
ACCENT_GOLD = "#f0c040"
ACCENT_SILVER = "#b0b8c0"
ACCENT_BRONZE = "#d4845a"
ACCENT_ORANGE = "#f78166"
LINK_BLUE = "#58a6ff"

# 布局基准值（实际渲染时 × SCALE）
_BW = 800   # 画布宽度
_BPX = 40   # 水平内边距
_BPT = 32   # 顶部内边距
_BPB = 24   # 底部内边距
_BHH = 88   # 头部高度
_BIH = 84   # 单个项目高度
_BFH = 62   # 底部高度
_BRS = 44   # 排名徽章尺寸
_BRR = 8    # 圆角半径
_BGS = 8    # 行内小间距

# 图标基准尺寸
_STAR_R = 7      # 五角星半径
_TRI_SIZE = 8    # 上升三角尺寸
_DOT_R = 5       # 语言圆点半径

# ── 字体查找 ──────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc",       # Windows
    "PingFang.ttc", "Heiti SC.ttf", "STHeiti.ttf",               # macOS
    "NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc",           # Linux
    "NotoSansSC-Regular.otf", "wqy-microhei.ttc", "wqy-zenhei.ttc",
    "DroidSansFallbackFull.ttf",
    "DejaVuSans.ttf", "arial.ttf",
]

_LATIN_FONT_CANDIDATES = [
    "segoeui.ttf", "arial.ttf",                       # Windows
    "Helvetica.ttc", "Arial.ttf",                     # macOS
    "DejaVuSans.ttf", "LiberationSans-Regular.ttf",   # Linux
    "NotoSans-Regular.ttf", "NotoSansMono-Regular.ttf",
]

Font = object  # ImageFont.FreeTypeFont | ImageFont.ImageFont


@dataclass(frozen=True)
class FontPair:
    """一组互补字体：CJK 字体负责非 ASCII，拉丁字体负责 ASCII。"""

    cjk: Font
    latin: Font


def _search_font_paths() -> list:
    candidates = []
    # 插件自带的 fonts/ 子目录优先（保证无字体的服务器也能渲染中文）
    candidates.append(Path(__file__).parent / "fonts")
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", "C:\\Windows")
        candidates.append(Path(windir) / "Fonts")
    elif sys.platform == "darwin":
        candidates.extend([Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                           Path.home() / "Library" / "Fonts"])
    else:
        candidates.extend([
            Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
            Path.home() / ".fonts", Path.home() / ".local/share/fonts",
        ])
    return candidates


@lru_cache(maxsize=1)
def _font_file_index() -> dict:
    """建立一次字体文件索引，避免每个字号重复遍历字体目录。"""
    index = {}
    for base in _search_font_paths():
        try:
            if not base.is_dir():
                continue
            for root, _dirs, files in os.walk(base):
                for filename in files:
                    index.setdefault(filename.lower(), Path(root) / filename)
        except OSError:
            continue
    return index


def _load_font(candidates: list, size: int) -> Font:
    index = _font_file_index()
    for name in candidates:
        path = index.get(Path(name).name.lower())
        try:
            return ImageFont.truetype(str(path) if path else name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def find_font(font_name: str, size: int) -> Font:
    return _load_font([font_name, *_FONT_CANDIDATES], size)


def find_font_pair(font_name: str, size: int) -> FontPair:
    return FontPair(
        cjk=find_font(font_name, size),
        latin=_load_font(_LATIN_FONT_CANDIDATES, size),
    )


# ── 文本绘制工具（ASCII/非 ASCII 分段回退）────────────────────────────────


def _font_runs(text: str, fonts: FontPair) -> Iterator:
    """按 ASCII/非 ASCII 分段，规避 Pillow 不自动字体回退的问题。"""
    if not text:
        return
    start = 0
    current_font = fonts.latin if text[0].isascii() else fonts.cjk
    for index, char in enumerate(text[1:], start=1):
        font = fonts.latin if char.isascii() else fonts.cjk
        if font is not current_font:
            yield text[start:index], current_font
            start = index
            current_font = font
    yield text[start:], current_font


def _text_width(text: str, fonts: FontPair) -> float:
    return sum(font.getlength(run) for run, font in _font_runs(text, fonts))


def _text_height(text: str, fonts: FontPair) -> int:
    heights = []
    for run, font in _font_runs(text, fonts):
        bbox = font.getbbox(run)
        heights.append(bbox[3] - bbox[1])
    return max(heights, default=0)


def _draw_text(draw, xy, text: str, fill: str, fonts: FontPair) -> None:
    x, y = xy
    for run, font in _font_runs(text, fonts):
        draw.text((x, y), run, fill=fill, font=font)
        x += font.getlength(run)


def _truncate_text(text: str, fonts: FontPair, max_width: int) -> str:
    if not text:
        return ""
    if _text_width(text, fonts) <= max_width:
        return text
    while len(text) > 0:
        text = text[:-1]
        if _text_width(text + "...", fonts) <= max_width:
            return text + "..."
    return "..."


# ── 手绘图标 ──────────────────────────────────────────────────────────────


def _get_medal_info(rank: int):
    if rank == 1:
        return ACCENT_GOLD, "#0d1117", "1"
    elif rank == 2:
        return ACCENT_SILVER, "#0d1117", "2"
    elif rank == 3:
        return ACCENT_BRONZE, "#0d1117", "3"
    return CARD_BG, TEXT_SECONDARY, str(rank)


def _draw_star(draw, cx: int, cy: int, r: int, fill: str):
    """手绘五角星。"""
    points = []
    for i in range(5):
        outer = i * 4 * math.pi / 5 - math.pi / 2
        inner = outer + 2 * math.pi / 10
        points.append((cx + r * math.cos(outer), cy + r * math.sin(outer)))
        points.append((cx + r * 0.38 * math.cos(inner), cy + r * 0.38 * math.sin(inner)))
    draw.polygon(points, fill=fill)


def _draw_triangle_up(draw, x: int, y: int, size: int, fill: str):
    """手绘上升三角（今日新增）。"""
    draw.polygon([(x + size / 2, y), (x, y + size), (x + size, y + size)], fill=fill)


# ── 主渲染 ────────────────────────────────────────────────────────────────


def render_trending(
    repos: list,
    language: str = "",
    trending_url: Optional[str] = None,
) -> bytes:
    """将 trending 数据渲染为高清 PNG 图片（2x 缩放）。

    Args:
        repos: RepoInfo 列表
        language: 过滤语言（用于副标题展示），空 = 全语言
        trending_url: 底部展示的榜单 URL

    Returns:
        PNG 图片的 bytes
    """
    s = SCALE

    W = _BW * s
    PX = _BPX * s
    PT = _BPT * s
    PB = _BPB * s
    HH = _BHH * s
    IH = _BIH * s
    FH = _BFH * s
    RS = _BRS * s
    BR = _BRR * s
    GS = _BGS * s
    STAR_R = _STAR_R * s
    TRI_SIZE = _TRI_SIZE * s
    DOT_R = _DOT_R * s

    font_title = find_font_pair("msyh.ttc", 26 * s)
    font_subtitle = find_font_pair("msyh.ttc", 22 * s)
    font_name = find_font_pair("msyh.ttc", 18 * s)
    font_desc = find_font_pair("msyh.ttc", 15 * s)
    font_lang = find_font_pair("msyh.ttc", 14 * s)
    font_stars = find_font_pair("msyh.ttc", 16 * s)
    font_rank = find_font_pair("msyh.ttc", 22 * s)
    font_footer = find_font_pair("msyh.ttc", 14 * s)

    # ── 画布 ─────────────────────────────────────────────────────────
    item_count = len(repos)
    content_h = item_count * IH
    total_h = PT + HH + content_h + FH + PB
    img = Image.new("RGB", (W, total_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = PT

    # ── 头部 ─────────────────────────────────────────────────────────
    _draw_text(draw, (PX, y), "GitHub Trending", fill=ACCENT_GOLD, fonts=font_title)
    subtitle = f"— Daily Top {item_count}"
    if language:
        subtitle += f" · {language}"
    tw = _text_width("GitHub Trending", font_title)
    _draw_text(draw, (PX + tw + GS, y + 4 * s), subtitle,
               fill=TEXT_SECONDARY, fonts=font_subtitle)

    now = datetime.now()
    weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    date_str = now.strftime(f"%Y-%m-%d {weekday_map[now.weekday()]}")
    _draw_text(draw, (PX, y + 40 * s), date_str, fill=TEXT_TERTIARY, fonts=font_subtitle)

    y += HH
    draw.line([(PX, y), (W - PX, y)], fill=CARD_BG, width=1)
    y += 12 * s

    # ── 项目列表 ─────────────────────────────────────────────────────
    name_max_w = W - PX * 2 - RS - 16 * s - 130 * s

    for repo in repos:
        cleft, cright = PX, W - PX

        # 排名徽章（前三名金银铜奖牌）
        badge_bg, badge_fg, badge_text = _get_medal_info(repo.rank)
        bx, by = cleft, y + (IH - RS) // 2
        if repo.rank <= 3:
            draw.ellipse([(bx, by), (bx + RS, by + RS)], fill=badge_bg)
        else:
            draw.rounded_rectangle([(bx, by), (bx + RS, by + RS)], radius=BR, fill=badge_bg)

        rw = _text_width(badge_text, font_rank)
        rh = _text_height(badge_text, font_rank)
        _draw_text(draw, (bx + (RS - rw) / 2, by + (RS - rh) / 2 - 6 * s),
                   badge_text, fill=badge_fg, fonts=font_rank)

        # 仓库名
        nx, ny = bx + RS + 16 * s, y + 8 * s
        name_disp = _truncate_text(repo.full_name, font_name, name_max_w)
        _draw_text(draw, (nx, ny), name_disp, fill=TEXT_PRIMARY, fonts=font_name)

        # 今日新增（右上角，上升三角 + 橙色数字）
        if repo.stars_today > 0:
            today_text = f"+{repo.stars_today} today"
            tw2 = _text_width(today_text, font_stars)
            tx = cright - tw2 - TRI_SIZE - 5 * s
            ty = ny + 6 * s
            _draw_triangle_up(draw, tx, ty, TRI_SIZE, ACCENT_ORANGE)
            _draw_text(draw, (cright - tw2, ny), today_text,
                       fill=ACCENT_ORANGE, fonts=font_stars)

        # 描述
        dy = ny + 28 * s
        if repo.description:
            desc_disp = _truncate_text(repo.description, font_desc, cright - nx)
            _draw_text(draw, (nx, dy), desc_disp, fill=TEXT_SECONDARY, fonts=font_desc)

        # 语言圆点 + 总星数（五角星图标）
        ly = dy + 26 * s
        lx = nx
        if repo.language:
            dot_color = repo.language_color or "#858585"
            draw.ellipse([(lx, ly + 6 * s), (lx + DOT_R * 2, ly + 6 * s + DOT_R * 2)],
                         fill=dot_color)
            _draw_text(draw, (lx + 16 * s, ly), repo.language,
                       fill=TEXT_TERTIARY, fonts=font_lang)
            lx += 16 * s + _text_width(repo.language, font_lang) + 18 * s

        stars_text = repo.stars_str
        _draw_star(draw, int(lx + STAR_R), int(ly + STAR_R + 5 * s), STAR_R, ACCENT_ORANGE)
        _draw_text(draw, (lx + STAR_R * 2 + 6 * s, ly), stars_text,
                   fill=ACCENT_ORANGE, fonts=font_stars)

        y += IH
        if repo.rank < item_count:
            draw.line([(cleft, y), (cright, y)], fill=CARD_BG, width=1)

    y += 10 * s

    # ── 底部 ─────────────────────────────────────────────────────────
    if trending_url is None:
        trending_url = "https://github.com/trending?since=daily"
    footer_text = f"共 {item_count} 个项目 · 按今日新增 Star 排序 · 数据来自 GitHub Trending"
    fw = _text_width(footer_text, font_footer)
    _draw_text(draw, ((W - fw) / 2, y), footer_text, fill=TEXT_TERTIARY, fonts=font_footer)
    y += 22 * s
    uw = _text_width(trending_url, font_footer)
    _draw_text(draw, ((W - uw) / 2, y), trending_url, fill=LINK_BLUE, fonts=font_footer)

    # ── 导出（带 DPI 元数据）─────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, dpi=(144, 144))
    return buf.getvalue()
