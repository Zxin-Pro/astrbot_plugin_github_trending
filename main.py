"""astrbot_plugin_github_trending — 每日 GitHub 星标涨幅 Top 10。

功能：
- /github            获取今日 GitHub 星标涨幅最高的十个项目（图片渲染，失败回退文本）
- /github refresh    强制刷新数据（清除缓存重新抓取）
- /github <语言>     按指定编程语言筛选，如 /github python
- /github help       显示帮助
- /github debug      诊断检查（网络/解析/翻译/渲染/调度）
- 每日定时推送：到 push_time 向 push_target 中的目标推送榜单
"""
from __future__ import annotations

import asyncio
import base64
import re
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:
    from .fetcher import RepoInfo, TrendingFetcher, build_trending_url
    from .renderer import render_trending
    from .translator import Translator
except ImportError:  # 允许直接以脚本方式调试
    from fetcher import RepoInfo, TrendingFetcher, build_trending_url
    from renderer import render_trending
    from translator import Translator

PLUGIN_NAME = "astrbot_plugin_github_trending"

HELP_TEXT = """📖 GitHub Trending 指令帮助

/github — 获取今日 GitHub 星标涨幅 Top 10
/github refresh — 强制刷新数据（忽略缓存）
/github <语言> — 按编程语言筛选，如 /github python /github rust
/github help — 显示本帮助
/github debug — 诊断检查（网络/解析/翻译/渲染/调度）

📌 定时推送在插件配置页设置：
· push_time — 每日推送时间（默认 09:00）
· push_target — 推送目标（群号/用户 ID，逗号分隔，推荐填 unified_msg_origin）
· language_filter — 默认语言过滤
· enable_image_render — 是否渲染图片
"""


@register(PLUGIN_NAME, "Zxin_Pro", "每日 GitHub 星标涨幅 Top 10 查询与定时推送", "1.0.0")
class GitHubTrendingPlugin(Star):
    """GitHub Trending 插件主类。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # AstrBot 会按 _conf_schema.json 注入 config；兼容旧版无配置注入的情况
        self._config: dict = config or {}
        proxy = str(self._config.get("proxy", "") or "")

        self._translator = Translator(proxy=proxy)
        self._fetcher = TrendingFetcher(translator=self._translator, proxy=proxy)

        self._scheduler_task: asyncio.Task | None = None
        self._running = False
        # 推送防重：{date_str: True}
        self._pushed_today = ""

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def initialize(self):
        """插件加载：启动定时推送任务。"""
        # 兼容处理：若宿主通过 context.get_config() 传的全局配置里带了本插件段，则合并
        try:
            global_cfg = self.context.get_config()
            plugin_cfg = (global_cfg.get("plugin", {}) or {}).get(PLUGIN_NAME, None) \
                if isinstance(global_cfg, dict) else None
            if isinstance(plugin_cfg, dict) and plugin_cfg:
                merged = {**plugin_cfg, **self._config}
                self._config = merged
        except Exception:
            pass

        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        push_time = self._normalize_time(self._get_cfg("push_time", "09:00"))
        targets = self._parse_targets()
        logger.info(
            f"[GitHubTrending] 插件已启动，推送时间 {push_time or '未配置'}，"
            f"推送目标 {len(targets)} 个，语言过滤 "
            f"'{self._get_cfg('language_filter', '') or 'all'}'"
        )

    async def terminate(self):
        """插件卸载：取消定时任务并关闭网络会话。"""
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        await self._fetcher.close()
        await self._translator.close()
        logger.info("[GitHubTrending] 插件已停止")

    # ── 配置辅助 ──────────────────────────────────────────────────────────

    def _get_cfg(self, key: str, default=""):
        """读取配置（容忍大小写差异与 None 值）。"""
        val = self._config.get(key, default)
        return default if val is None else val

    @staticmethod
    def _normalize_time(raw) -> str:
        """将时间规范化为 HH:MM 零填充格式，'9:00' → '09:00'。"""
        raw = str(raw or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", raw)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
        return raw

    def _parse_targets(self) -> list:
        """解析推送目标：逗号分隔，剔除空白项。"""
        raw = str(self._get_cfg("push_target", "") or "").strip()
        if not raw:
            return []
        return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]

    def _max_items(self) -> int:
        try:
            return max(1, min(25, int(self._get_cfg("max_items", 10))))
        except (ValueError, TypeError):
            return 10

    def _image_enabled(self) -> bool:
        return bool(self._get_cfg("enable_image_render", True))

    def _build_translator(self):
        """按当前配置同步翻译器状态，翻译关闭时返回 None。"""
        proxy = str(self._get_cfg("proxy", "") or "")
        self._translator.update_proxy(proxy)
        self._fetcher._proxy = proxy
        enabled = bool(self._get_cfg("translate_enabled", True))
        return self._translator if enabled else None

    # ── 数据获取与消息构造 ────────────────────────────────────────────────

    async def _get_repos(self, language: str = "", refresh: bool = False) -> list:
        """获取榜单数据（已按今日新增星数降序），并按 max_items 截断。"""
        translator = self._build_translator()
        self._fetcher._translator = translator
        if refresh:
            self._fetcher.clear_cache()
        repos = await self._fetcher.fetch(language=language, use_cache=not refresh)
        return repos[: self._max_items()]

    @staticmethod
    def _build_text(repos: list, language: str = "") -> str:
        """构造回退用的格式化文本消息。"""
        title = f"🔥 GitHub Trending 今日星标涨幅 Top {len(repos)}"
        if language:
            title += f"（{language}）"
        lines = [title, ""]
        for r in repos:
            lines.append(f"{r.rank}. {r.full_name}  ⭐ {r.stars_str}（今日 +{r.stars_today}）")
            if r.description:
                lines.append(f"    {r.description}")
            meta = []
            if r.language:
                meta.append(f"语言: {r.language}")
            lines.append("    " + " · ".join(meta) if meta else "")
            lines.append(f"    {r.url}")
            lines.append("")
        lines.append(f"🔗 完整榜单: {build_trending_url('daily', language)}")
        return "\n".join(lines)

    async def _render_image(self, repos: list, language: str) -> bytes | None:
        """渲染排行榜图片，失败返回 None。"""
        if not self._image_enabled():
            return None
        try:
            return render_trending(repos, language=language)
        except Exception as e:
            logger.warning(f"[GitHubTrending] 图片渲染失败，回退文本消息: {e}")
            return None

    async def _send_chain(self, umo: str, chain):
        """通过 unified_msg_origin 发送消息链。"""
        from astrbot.core.message.message_event_result import MessageChain

        await self.context.send_message(umo, chain)

    async def _send_to_target(self, umo: str, repos: list, language: str) -> None:
        """向指定目标发送榜单（图片优先，文本兜底）。"""
        image_bytes = await self._render_image(repos, language)
        from astrbot.core.message.message_event_result import MessageChain

        chain = MessageChain()
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            chain.chain = [Plain("🔥 GitHub 今日星标涨幅榜\n"), Image.fromBase64(b64)]
        else:
            chain.chain = [Plain(self._build_text(repos, language))]
        await self._send_chain(umo, chain)

    # ── 定时推送 ──────────────────────────────────────────────────────────

    async def _scheduler_loop(self):
        """每 30 秒轮询，到达 push_time 触发一次当日推送（带防重）。"""
        while self._running:
            try:
                push_time = self._normalize_time(self._get_cfg("push_time", "09:00"))
                now = datetime.now()
                today_str = now.strftime("%Y-%m-%d")

                if (
                    self._get_cfg("push_enabled", True)
                    and re.match(r"^\d{2}:\d{2}$", push_time)
                    and now.strftime("%H:%M") == push_time
                    and self._pushed_today != today_str
                ):
                    targets = self._parse_targets()
                    if targets:
                        self._pushed_today = today_str  # 先标记防重，失败不重试
                        asyncio.create_task(self._do_push(targets))
                    else:
                        logger.warning(
                            "[GitHubTrending] 到达推送时间但 push_target 为空，跳过推送"
                        )
                        self._pushed_today = today_str

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[GitHubTrending] 调度器异常")
                await asyncio.sleep(30)

    async def _do_push(self, targets: list):
        """执行一次定时推送。"""
        language = str(self._get_cfg("language_filter", "") or "").strip()
        try:
            repos = await self._get_repos(language=language)
        except Exception:
            logger.exception("[GitHubTrending] 定时推送获取数据失败")
            return
        logger.info(
            f"[GitHubTrending] ⏰ 定时推送触发，{len(repos)} 个项目 → {len(targets)} 个目标"
        )
        for umo in targets:
            try:
                await self._send_to_target(umo, repos, language)
                logger.info(f"[GitHubTrending] 已推送到 {umo}")
            except Exception:
                logger.exception(f"[GitHubTrending] 推送到 {umo} 失败")

    # ── 指令入口 ──────────────────────────────────────────────────────────

    @filter.command("github")
    async def github_cmd(self, event: AstrMessageEvent):
        """GitHub Trending 主指令，子命令分发。"""
        args = (event.message_str or "").strip().split()
        sub = args[1].lower() if len(args) > 1 else ""

        if sub in ("help", "帮助"):
            yield event.plain_result(HELP_TEXT)
            return
        if sub == "debug":
            async for msg in self._run_diagnostics(event):
                yield msg
            return

        # /github refresh 或 /github python（语言参数即临时过滤）
        refresh = sub == "refresh"
        language = "" if refresh else (args[1].strip() if len(args) > 1 else "")
        if language.lower() in ("all", "全部"):
            language = ""

        yield event.plain_result("🔍 正在获取 GitHub Trending 榜单...")
        try:
            repos = await self._get_repos(language=language, refresh=refresh)
        except Exception as e:
            logger.exception("[GitHubTrending] 获取榜单失败")
            yield event.plain_result(f"❌ 获取榜单失败: {e}")
            return

        if not repos:
            yield event.plain_result("❌ 今日榜单数据为空，请稍后重试（可尝试 /github debug）")
            return

        image_bytes = await self._render_image(repos, language)
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            yield event.chain_result([
                Plain(f"🔥 GitHub 今日星标涨幅 Top {len(repos)}\n"),
                Image.fromBase64(b64),
            ])
        else:
            yield event.plain_result(self._build_text(repos, language))

    # ── 诊断 ──────────────────────────────────────────────────────────────

    async def _run_diagnostics(self, event: AstrMessageEvent):
        """逐项检查：配置 → 网络 → 解析 → 翻译 → 渲染 → 调度。"""
        yield event.plain_result("🩺 开始诊断检查...")

        report = []

        # 1. 配置
        push_time = self._normalize_time(self._get_cfg("push_time", "09:00"))
        time_ok = bool(re.match(r"^\d{2}:\d{2}$", push_time))
        targets = self._parse_targets()
        report.append(
            f"{'✅' if time_ok else '❌'} 配置: push_time={push_time or '未设置'}"
            f"{'' if time_ok else '（格式应为 HH:MM）'}，"
            f"push_target={len(targets)} 个，max_items={self._max_items()}，"
            f"language_filter='{self._get_cfg('language_filter', '') or 'all'}'"
        )

        # 2. 网络 + 解析（真实抓一次页面）
        language = str(self._get_cfg("language_filter", "") or "").strip()
        try:
            html = await self._fetcher._fetch_html(language)
            repos = self._fetcher._parse_html(html)
            if repos:
                top = repos[0]
                report.append(
                    f"✅ 网络/解析: 抓取 {len(html) // 1024}KB，"
                    f"解析到 {len(repos)} 个仓库，Top1 {top.full_name} "
                    f"(+{top.stars_today} today, {top.stars_str} stars)"
                )
            else:
                report.append("❌ 解析: 页面抓取成功但未解析到仓库，GitHub 页面结构可能已变更")
        except Exception as e:
            report.append(f"❌ 网络: {e}")
            repos = []

        # 3. 翻译
        if self._get_cfg("translate_enabled", True):
            ok = await self._translator.probe()
            channel = self._translator.last_provider
            if ok:
                report.append(f"✅ 翻译: 生效通道 {channel}（google 失败会自动切 mymemory）")
            else:
                report.append("⚠️ 翻译: 所有通道均不可达（将保留英文原文，不影响使用）")
        else:
            report.append("⏭️ 翻译: 已关闭")

        # 4. 渲染
        if self._image_enabled() and repos:
            try:
                img = render_trending(repos[:3], language=language)
                report.append(f"✅ 渲染: 图片生成正常（{len(img) // 1024}KB，2x 缩放）")
            except Exception as e:
                report.append(f"⚠️ 渲染: {e}（将回退文本消息，需检查 Pillow/字体）")
        elif not self._image_enabled():
            report.append("⏭️ 渲染: 已关闭（文本输出）")
        else:
            report.append("⏭️ 渲染: 无数据，跳过")

        # 5. 调度
        sched = "运行中" if (self._scheduler_task and not self._scheduler_task.done()) else "未运行"
        report.append(
            f"{'✅' if sched == '运行中' else '❌'} 调度: {sched}，"
            f"推送开关 {'开' if self._get_cfg('push_enabled', True) else '关'}，"
            f"今日{'已' if self._pushed_today == datetime.now().strftime('%Y-%m-%d') else '未'}推送"
        )

        yield event.plain_result("\n".join(report))
