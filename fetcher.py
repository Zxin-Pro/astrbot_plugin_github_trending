"""GitHub Trending 数据获取层。

直接抓取 GitHub Trending 页面 (https://github.com/trending?since=daily)，
用 BeautifulSoup 解析 HTML，提取仓库全名、URL、描述、语言、总星数、今日新增星数，
并按「今日新增星数」降序排列。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger("astrbot")

TRENDING_URL = "https://github.com/trending"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def build_trending_url(since: str = "daily", language: str = "") -> str:
    """构造 GitHub Trending 页面 URL。"""
    language = (language or "").strip()
    path = TRENDING_URL
    if language:
        path += f"/{quote(language, safe='')}"
    return f"{path}?{urlencode({'since': since})}"


# 语言 → 显示色（GitHub 官方 linguist 配色，节选常用项）
LANGUAGE_COLORS: dict = {
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "Java": "#b07219",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "C++": "#f34b7d",
    "C": "#555555",
    "C#": "#178600",
    "Ruby": "#701516",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "PHP": "#4F5D95",
    "Vue": "#41b883",
    "Shell": "#89e051",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Jupyter Notebook": "#DA5B0B",
    "Dart": "#00B4AB",
    "Scala": "#c22d40",
    "Lua": "#000080",
    "R": "#198CE7",
    "Zig": "#ec915c",
    "Haskell": "#5e5086",
    "MDX": "#fcb32c",
    "Dockerfile": "#384d54",
    "Makefile": "#427819",
    "CMake": "#DA3434",
    "Objective-C": "#438eff",
    "Astro": "#ff5a03",
}


@dataclass
class RepoInfo:
    """单个仓库的展示信息。"""

    rank: int
    owner: str
    repo: str
    url: str
    description: str = ""
    language: str = ""
    language_color: str = ""
    stars: int = 0
    stars_str: str = ""
    stars_today: int = 0
    stars_today_str: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class TrendingFetcher:
    """GitHub Trending 数据获取器（抓取 + 解析 + 缓存）。"""

    def __init__(self, translator=None, proxy: str = ""):
        self._translator = translator
        self._proxy = proxy
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_ttl = 300  # 5 分钟缓存，refresh 指令可强制清空

    # ── 会话管理 ──────────────────────────────────────────────────────────

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 缓存 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(language: str = "") -> str:
        return f"trending_daily_{(language or 'all').strip().lower()}"

    def _get_cached(self, language: str = "") -> Optional[list]:
        entry = self._cache.get(self._cache_key(language))
        if entry:
            data, ts = entry
            if time.time() - ts < self._cache_ttl:
                return data
            self._cache.pop(self._cache_key(language), None)
        return None

    def _set_cache(self, language: str, data: list) -> None:
        self._cache[self._cache_key(language)] = (data, time.time())

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── 格式化工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_int(text: str) -> int:
        """解析 "2,194" 格式的数字。"""
        try:
            return int(re.sub(r"[^\d]", "", text))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _format_stars(count: int) -> str:
        """将 star 数格式化为人类可读形式，如 12.3k。"""
        if count >= 1000:
            k = count / 1000
            if k >= 100:
                return f"{int(k)}k"
            return f"{k:.1f}k"
        return str(count)

    @staticmethod
    def _format_description(desc: str, max_len: int = 80) -> str:
        desc = (desc or "").strip()
        if len(desc) > max_len:
            return desc[: max_len - 3] + "..."
        return desc

    # ── 抓取 ──────────────────────────────────────────────────────────────

    async def _fetch_html(self, language: str = "") -> str:
        """获取 GitHub Trending 每日页面 HTML。"""
        await self._ensure_session()
        url = build_trending_url("daily", language)
        kwargs = {"headers": _HEADERS, "timeout": aiohttp.ClientTimeout(total=20)}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        try:
            async with self._session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise RuntimeError(f"GitHub 返回 HTTP {resp.status}: {body}")
                return await resp.text()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"网络请求失败: {e}") from e

    # ── 解析 ──────────────────────────────────────────────────────────────

    def _parse_html(self, html: str) -> list:
        """解析 Trending 页面 HTML，提取仓库列表（页面原始顺序）。"""
        # 优先 lxml（更快更稳），环境缺失时回退内置 parser
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        articles = soup.find_all("article", class_="Box-row")
        repos: list = []
        rank = 0

        for article in articles:
            rank += 1

            # ── 仓库名称：h2 > a[href="/owner/repo"] ─────────────────────
            name_link = None
            h2 = article.find("h2")
            if h2:
                name_link = h2.find("a", href=True)
            if not name_link:
                name_link = article.find("a", href=re.compile(r"^/[^/]+/[^/]+$"))
            if not name_link:
                continue

            href = name_link.get("href", "").strip()
            parts = href.strip("/").split("/")
            if len(parts) < 2:
                continue
            owner, repo = parts[0].strip(), parts[1].strip()

            # ── 描述 ─────────────────────────────────────────────────────
            desc_el = article.find("p", class_=re.compile(r"col-9|color-fg-muted|my-1"))
            description = desc_el.get_text(strip=True) if desc_el else ""

            # ── 语言 ─────────────────────────────────────────────────────
            language = ""
            language_color = ""
            lang_name_el = article.find("span", itemprop="programmingLanguage")
            if lang_name_el:
                language = lang_name_el.get_text(strip=True)
            lang_color_el = article.find("span", class_="repo-language-color")
            if lang_color_el:
                m = re.search(r"#([0-9a-fA-F]{6})", lang_color_el.get("style", ""))
                if m:
                    language_color = f"#{m.group(1)}"

            # ── 总星数：a[href$="/stargazers"] ──────────────────────────
            stars = 0
            stars_link = article.find("a", href=re.compile(r"/stargazers$"))
            if stars_link:
                stars = self._parse_int(stars_link.get_text(strip=True))

            # ── 今日新增星数："X,XXX stars today" ───────────────────────
            stars_today = 0
            today_el = article.find("span", class_="float-sm-right")
            if today_el:
                today_text = today_el.get_text(strip=True)
                m = re.search(r"([\d,]+)\s*stars?\s+today", today_text)
                if m:
                    stars_today = self._parse_int(m.group(1))

            repos.append(
                RepoInfo(
                    rank=rank,
                    owner=owner,
                    repo=repo,
                    url=f"https://github.com/{owner}/{repo}",
                    description=self._format_description(description),
                    language=language,
                    language_color=LANGUAGE_COLORS.get(language, language_color),
                    stars=stars,
                    stars_str=self._format_stars(stars),
                    stars_today=stars_today,
                    stars_today_str=self._format_stars(stars_today),
                )
            )

        return repos

    # ── 翻译 ──────────────────────────────────────────────────────────────

    async def _translate_descriptions(self, repos: list) -> None:
        """批量翻译仓库描述，失败静默保留原文。"""
        texts = [r.description for r in repos if r.description]
        if not texts or not self._translator:
            return
        try:
            translated = await self._translator.translate_batch(texts)
        except Exception as e:
            logger.warning(f"[GitHubTrending] 翻译失败，保留英文原文: {e}")
            return
        idx = 0
        for r in repos:
            if r.description:
                if idx < len(translated) and translated[idx]:
                    r.description = translated[idx]
                idx += 1

    # ── 公开接口 ──────────────────────────────────────────────────────────

    async def fetch(self, language: str = "", use_cache: bool = True) -> list:
        """获取今日 Trending 数据，按「今日新增星数」降序排列。

        Args:
            language: 编程语言过滤，如 "python"。空 = 全语言。
            use_cache: 是否使用 5 分钟缓存。

        Returns:
            RepoInfo 列表（调用方按 max_items 截断）。
        """
        if use_cache:
            cached = self._get_cached(language)
            if cached is not None:
                return cached

        html = await self._fetch_html(language)
        repos = self._parse_html(html)

        if not repos:
            raise RuntimeError(
                f"未能从 GitHub Trending 页面解析到任何仓库"
                f"（language={language or 'all'}），页面结构可能已变更"
            )

        # 按「今日新增星数」降序，取涨幅最高的项目
        repos.sort(key=lambda r: r.stars_today, reverse=True)
        for i, r in enumerate(repos, start=1):
            r.rank = i

        if self._translator:
            await self._translate_descriptions(repos)

        self._set_cache(language, repos)
        return repos
