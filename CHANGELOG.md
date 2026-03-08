# 更新日志

## v1.4.0

### 新增

- 新增小黑盒平台支持，并同步更新 README 支持列表。
- 新增 `core/parsers/xiaoheihe.py`，支持：
  - 小黑盒帖子链接与分享卡片解析
  - 小黑盒游戏详情页链接与分享卡片解析
- 新增标准内容类型 `TextContent`，用于承载独立文本消息项。
- 新增 `create_video_content_by_task(...)`，支持基于自定义下载任务构造视频内容。
- 新增 `ytdlp_download_video_relaxed(...)`，用于宽松处理特定视频下载场景。
- 新增小黑盒配置项：
  - `show_body_text`
  - `video_send_mode`

### 变更

- 扩展配置结构，正式纳入小黑盒解析器配置节点。
- 扩展 sender，对 `TextContent` 提供标准发送支持。
- 调整小黑盒视频发送策略：视频内容从合并转发中分离，改为单独发送。
- 调整小黑盒视频输出策略，支持以下模式：
  - `不发送视频`
  - `发送第一条视频`
  - `发送全部视频`
- 调整小黑盒 `.m3u8` 视频处理路径，改为使用宽松 yt-dlp 下载逻辑。
- 增强小黑盒游戏页解析结果，纳入图片、结构化文本与视频提取能力。

### 涉及文件

- `README.md`
- `_conf_schema.json`
- `core/config.py`
- `core/data.py`
- `core/download.py`
- `core/parsers/__init__.py`
- `core/parsers/base.py`
- `core/parsers/xiaoheihe.py`
- `core/sender.py`
- `default_template.json`
- `metadata.yaml`

### 说明

本次升级（包括小黑盒网页逆向）由 GPT 5.4 驱动的 AI 智能体协助完成。
本次升级已通过人工验收，不会影响任何插件已有的功能。