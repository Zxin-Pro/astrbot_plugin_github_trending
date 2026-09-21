"""翻译模块 — 免费翻译接口，多通道自动降级。

通道顺序：
1. Google Translate 免费接口（translate.googleapis.com，质量最好，需网络可达）
2. MyMemory 免费接口（api.mymemory.translated.net，国内可直连，单条 ≤500 字符）

每个文本按通道顺序尝试，一旦某通道成功会记住该通道优先使用；
全部失败时返回空串，由上层静默保留英文原文。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("astrbot")

# ── 通道定义 ──────────────────────────────────────────────────────────────

_GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"

_MYMEMORY_URL = "https://api.mymemory.translated.net/get"

# 纯数字/符号/链接无需翻译
_NO_TRANSLATE_RE = re.compile(r"^[\d\s.,;:!?@#$%^&*()\[\]{}/\\<>|\-_+=~`'\"➕→←↑↓✓✗•·©®™]+$")

_MAX_TEXT_LEN = 480  # MyMemory 匿名单条上限 500 字符，留余量


class Translator:
    """轻量级翻译器：多通道降级 + 内存缓存。"""

    def __init__(self, source: str = "en", target: str = "zh-CN", proxy: str = ""):
        self.source = source
        self.target = target
        self._proxy = proxy
        self._cache: dict = {}
        self._session: Optional[aiohttp.ClientSession] = None
        # 上一次成功的通道名（-1 = 尚未探测，优先按列表顺序）
        self._preferred: int = -1
        self.provider_names = ["google", "mymemory"]

    # ── 会话 / 配置 ───────────────────────────────────────────────────────

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def update_proxy(self, proxy: str) -> None:
        if proxy != self._proxy:
            self._proxy = proxy
            self._cache.clear()
            self._preferred = -1

    @property
    def last_provider(self) -> str:
        if 0 <= self._preferred < len(self.provider_names):
            return self.provider_names[self._preferred]
        return "未探测"

    # ── 各通道实现 ────────────────────────────────────────────────────────

    def _common_kwargs(self) -> dict:
        kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return kwargs

    async def _via_google(self, text: str) -> str:
        await self._ensure_session()
        params = {
            "client": "gtx",
            "sl": self.source,
            "tl": self.target,
            "dt": "t",
            "q": text,
        }
        async with self._session.get(_GOOGLE_URL, params=params, **self._common_kwargs()) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        # 响应格式: [[["译文","原文",...],...],...]
        if not data or not isinstance(data[0], list):
            raise RuntimeError("响应格式异常")
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        if not translated:
            raise RuntimeError("返回空译文")
        return translated

    async def _via_mymemory(self, text: str) -> str:
        await self._ensure_session()
        params = {
            "q": text[:_MAX_TEXT_LEN],
            "langpair": f"{self.source}|{self.target}",
        }
        async with self._session.get(_MYMEMORY_URL, params=params, **self._common_kwargs()) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        translated = (data.get("responseData") or {}).get("translatedText", "")
        # MyMemory 对无结果/异常会返回提示文本，需过滤
        if not translated or "MYMEMORY WARNING" in translated.upper() \
                or "QUERY LENGTH LIMIT" in translated.upper() \
                or translated.strip().lower() == text.strip().lower():
            raise RuntimeError(f"无有效译文: {translated[:60]}")
        return translated

    # ── 公开接口 ──────────────────────────────────────────────────────────

    async def probe(self) -> bool:
        """诊断用：探测翻译链路（含通道降级）是否可用。"""
        try:
            return bool(await self.translate("hello world"))
        except Exception:
            return False

    async def translate(self, text: str) -> str:
        """翻译单段文本，多通道自动降级。空文本或纯数字/符号直接返回原文。"""
        text = (text or "").strip()
        if not text or _NO_TRANSLATE_RE.match(text):
            return text

        cache_key = f"{self.source}|{self.target}|{text}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        await self._ensure_session()

        # 通道顺序：优先上一次成功的通道，其余按定义顺序
        order = list(range(len(self.provider_names)))
        if self._preferred >= 0:
            order.remove(self._preferred)
            order.insert(0, self._preferred)

        last_err = None
        for idx in order:
            try:
                if idx == 0:
                    translated = await self._via_google(text)
                else:
                    translated = await self._via_mymemory(text)
                self._preferred = idx
                self._cache[cache_key] = translated
                # 缓存防膨胀：超限时丢弃最早写入的一半
                if len(self._cache) > 500:
                    for k in list(self._cache)[: len(self._cache) - 400]:
                        self._cache.pop(k, None)
                return translated
            except Exception as e:
                last_err = e
                logger.debug(f"[GitHubTrending] 翻译通道 {self.provider_names[idx]} 失败: {e}")

        raise RuntimeError(f"所有翻译通道均失败: {last_err}")

    async def translate_batch(self, texts: list, batch_size: int = 20) -> list:
        """批量翻译，按 batch_size 分组并发，失败条目返回空串（上层保留原文）。"""
        import asyncio

        results: list = [""] * len(texts)
        pending: list = []

        # 先吃缓存
        for i, t in enumerate(texts):
            cache_key = f"{self.source}|{self.target}|{t.strip()}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[i] = cached
            else:
                pending.append(i)

        async def _safe(t: str) -> str:
            try:
                return await self.translate(t)
            except Exception:
                return ""

        for start in range(0, len(pending), batch_size):
            group = pending[start: start + batch_size]
            done = await asyncio.gather(*[_safe(texts[i]) for i in group])
            for i, translated in zip(group, done):
                if translated:
                    results[i] = translated
        return results
