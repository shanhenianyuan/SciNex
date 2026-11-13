# SciNex · 前沿情报追踪模块（Frontier Intel）设计规格

- 日期：2026-11-11
- 状态：已与用户确认方向，含两点补充（重点跟踪项 / 精读改手动）
- 项目：/opt/remote_ai/Paper-agent（单文件前端 paper-reader.html + 后端 serve.py）

## 1. 定位
在第 4 侧栏页「情报」提供全球大模型/智能体技术动态的每日追踪：
每日定时（默认 00:30 北京）自动抓取 arXiv 论文 + HuggingFace 模型 + GitHub 代码，
用 LLM 结构化为「发了什么成果 / 针对什么问题 / 解决了什么」，按热度排序呈现，
用户自行点选精读（呈现到现有阅读窗口），并逐渐累积成跨天知识网。

## 2. 五步闭环
1. 抓取：arXiv API（多关键词 + submittedDate desc 近几日）+ HF API（models/trending）+ GitHub API（关注 repo/搜索）。
2. 分析：内网 deepseek（/llm/v1）对每条产出 {released=?成果, problem=针对问题, solved=解决了什么, topic=主题, hot=热度0-10}。
3. 建网：节点=情报条目，按主题/方法/引用连边，daily 增量并入 graph.json。
4. 呈现：新 tab「情报」，今日 Top 榜 + 热点主题 + 重点跟踪专栏 + 知识网图 + 按日回看。
5. 精读：用户点选（排序由热度/时间给出），关联 arXiv 论文经 /dlpdf/v1 打开到主阅读区 + 深析文进问答窗口。

## 3. 用户确定的决策
- 数据源：arXiv 论文 + HuggingFace 模型 + GitHub 代码（可验证、覆盖全）。
- 调度：serve.py 内置后台定时线程（自包含、跟随部署，不依赖外部 cron）。
- 存储：`/data/qzq/sci_research`（91 宿主）== 容器 `/opt/remote_ai/sci_research`；本机默认 `./sci_research`。经 env `INTEL_DIR` 配置，不写死。
- 精读时机：呈现按热度/时间排序，用户自行点选精读；不自动打开窗口。
- 定时：`00:30` 北京时间，经 env `INTEL_TIME` 可改。

## 4. 新增能力：重点跟踪项
- 配置 `sci_research/watch.json`：数组，每项 {name, kind(repo|model|org|topic), keywords[], sources[]}。
- 用户可在 UI 添加/删除重点项（表单 + 列表）。
- 抓取时对每个重点项单独检索最新进展（GitHub repo 更新/releases、HF 模型、arXiv 相关），聚合到「重点跟踪」专栏。
- 预置示例项（后续可改）：vLLM、DeepSeek、Qwen、OpenAI、Meta-Llama 等。

## 5. 存储结构
```
sci_research/
  raw/YYYY-MM-DD.json       原始抓取（arxiv/hf/gh 三源原始列表）
  analysis/YYYY-MM-DD.json  分析结果（结构化字段 + watch 聚合 + hot 排序）
  graph.json                累积知识网（nodes/edges/主题）
  watch.json                重点跟踪项配置
  meta.json                 最近运行时间/状态/版本
```

## 6. 调度
- main() 起后台守护线程；每天 INTEL_TIME 检查「当天是否已跑」，未跑跑全流程；启动时今天没跑则补跑；重入保护（meta.json 锁）。
- 端点 POST /intel/v1/run 手动触发；GET /intel/v1/status 看状态。

## 7. 后端端点（服务端重启生效）
- GET  /intel/v1/status       最近运行状态
- GET  /intel/v1/daily?date=   某日快报/分析
- GET  /intel/v1/graph        累积知识网
- GET  /intel/v1/watch        读重点跟踪配置
- POST /intel/v1/watch        更新重点跟踪项（增删改）
- POST /intel/v1/run          手动立即跑一次（后台/等待）
- GET  /intel/v1/deep?id=     按需精读单条（复用深析 prompt，避开 send() 触发词）

## 8. 前端 UI（第 4 侧栏页）
- `.tabs` 加 button#t4「情报」，tab(4)=真；.pane#p4。
- 内容：状态条（最近运行/条数）→ 重点跟踪专栏（分组）→ 今日 Top 榜（热度排序，含成果/问题/解决 + 精读按钮）→ 热点主题标签 → 知识网图（复用 kgRender SVG 能力）→ 历史日期回看。
- 精读按钮：arXiv 关联 → /dlpdf/v1 打开 PDF 到主阅读区 + 深析进问答 tab；纯模型/仓库 → 情报页弹精读卡片。
- 重点跟踪管理：添加/删除表单。

## 9. 网络与安全（铁律）
- 抓取走 _build_opener()（有 OUTBOUND_PROXY 走隧道、无直连）；内网 LLM 恒直连。
- 不写死 IP/路径；INTEL_DIR & INTEL_TIME 走 env。
- LLM key 经 .env 注入，日志不打 key 值。
- 只写 sci_research 目录，不碰 /data/qzq 其他内容。

## 10. 验证
- py_compile + 单测（stub 网络，断言 daily/graph JSON 结构）。
- 本机起实例 POST /intel/v1/run 实测抓取+分析+知识网落盘。
- 91 重建容器后 UI 验证 tab、Top 榜、重点跟踪、精读进阅读窗口。
