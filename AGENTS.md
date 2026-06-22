# AGENTS.md｜IPTV播放列表检测

## 1. 项目信息
- 项目名称：IPTV播放列表检测
- 项目代号：iptv-check
- 项目类型：工具 / CLI
- 当前项目状态：已立项，待启动
- 当前轮次：第一轮
- 当前优先级：中

## 2. 项目目标
- 核心目标：检测 IPTV M3U 播放列表中每条流的可达性和播放效果
- 目标用户：本人（维护自用 IPTV 列表）
- 主要场景：将 GitHub 上的 M3U 播放列表拉下来，逐条检测流是否可用
- 成功标准：输入 GitHub 共享链接即可输出一份完整的可达性报告

## 3. 本轮范围
### 必做（In Scope）
- M3U 文件解析（频道名、URL、分组、Logo）
- DNS 解析 + TCP 连接可达性
- HTTP 响应状态码 + 响应时间
- 支持 GitHub 共享链接自动转 raw
- 并发检测（可配置）
- Markdown / JSON 两种报告格式

### 暂不做（Out of Scope）
- 图形界面
- 自动定时检测
- 流协议深测（RTMP、UDP 等）

## 4. 技术基线
- 语言：Python 3.8+
- 依赖：零，纯 stdlib
- 并发：ThreadPoolExecutor
- 报告：Markdown / JSON
- 部署：Mac 本地运行

## 5. 初始化摘要
- 已创建 `iptv-check.py` 初始草稿，待项目线程中继续打磨
- 未完成的开发工作请在项目线程中继续

## 6. 任务编号
- TASK-01：确认需求范围和输出格式
- TASK-02：M3U 解析模块
- TASK-03：流检测核心逻辑
- TASK-04：报告输出
- TASK-05：GitHub URL 支持
- TASK-06：测试和打磨
