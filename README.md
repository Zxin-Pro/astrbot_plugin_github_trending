# astrbot_plugin_github_trending

获取每日 GitHub 星标涨幅最高的十个项目，支持手动查询与每日定时推送。

## 功能

- **手动查询**：`/github` 获取今日星标涨幅 Top 10（按「今日新增 Star」降序）
- **语言筛选**：`/github python`、`/github rust` 等按编程语言过滤
- **强制刷新**：`/github refresh` 忽略缓存重新抓取
- **定时推送**：每日到点自动推送到指定群聊/私聊
- **图片渲染**：深色 GitHub 风格排行榜图片（2x 高清 1600px，手绘图标零 emoji 依赖），渲染失败自动回退格式化文本
- **中文翻译**：Google 免费接口自动翻译描述，失败静默保留原文
- **诊断检查**：`/github debug` 逐项检查网络/解析/翻译/渲染/调度

## 指令

| 指令 | 说明 |
|------|------|
| `/github` | 获取今日 GitHub 星标涨幅 Top 10 |
| `/github refresh` | 强制刷新数据 |
| `/github <语言>` | 按编程语言筛选，如 `/github python` |
| `/github help` | 显示帮助 |
| `/github debug` | 诊断检查 |

## 配置（WebUI 插件配置页）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `push_enabled` | `true` | 启用每日定时推送 |
| `push_time` | `09:00` | 每日推送时间（HH:MM） |
| `push_target` | 空 | 推送目标，群号/用户 ID，逗号分隔；推荐填 `unified_msg_origin`（如 `aiocqhttp:GroupMessage:123456`） |
| `language_filter` | 空 | 默认语言过滤，留空为全部 |
| `max_items` | `10` | 榜单条目数 |
| `enable_image_render` | `true` | 启用图片渲染 |
| `translate_enabled` | `true` | 中文翻译 |
| `proxy` | 空 | HTTP/HTTPS/SOCKS5 代理 |

### push_target 说明

推送通过 `context.send_message(unified_msg_origin, chain)` 实现，因此推荐填写完整
UMO 格式 `平台:消息类型:ID`（在任意一条消息日志中可见）。只填纯数字 ID 时
AstrBot 无法确定平台实例，建议优先使用完整格式。

## 架构

```
main.py        插件入口：指令注册、定时调度、诊断
fetcher.py     数据层：抓取 github.com/trending + BeautifulSoup 解析 + 缓存
renderer.py    渲染层：Pillow 2x 高清深色风格排行榜
translator.py  翻译层：Google 免费接口批量翻译
```

数据来源为 GitHub Trending 网页实时抓取（5 分钟缓存），与网站数据完全同步。
