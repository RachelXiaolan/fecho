# 技术债

先记下、暂时不做的事。按框架环节标注：①采集 ②归属 ③存储 ④整理 ⑤交付 ⑥登录·运行时。

## ① Cursor 不能每晚扫描（2026-09-16 记）

**现状**：Cursor 只能随手记（agent 自己调 `log_progress`），没有扫描补漏，也就没法交叉验证它漏记了什么。
网页上 Cursor 的扫描开关是灰的。线上 MCP 连接里 Cursor 排第二（80 次），用的人不少。

**记录其实能读**：`~/.cursor/projects/<文件夹名>/agent-transcripts/<会话>/<会话>.jsonl`，
每行 `{"role": ..., "message": {"content": [...]}}`，和 Claude / Codex 的差不多。

**缺的三件事**：

1. **每句话没有时间**。只能看文件修改时间，按北京时间切到某一天不准（跨天的会话会整段算到最后那天）。
2. **没有工作文件夹的完整路径**。文件夹名是把 `/` 换成 `-`（`Users-luchunlan-Documents-work`），
   路径里本来带 `-` 的还原不回来，白名单对不准。可能要从 `agent-tools/`、`terminals/` 里找 cwd，或者反过来拿白名单路径做同样的替换去比。
3. **本机没有能在后台叫醒的 Cursor**。按「每个 agent 扫自己的对话、用自己的配置」，要装 Cursor 官方的
   `cursor-agent` 命令行（`cursor-agent -p`），并实测它在后台（launchd / 计划任务）叫不叫得醒、登录态在不在。

**做的时候要改**：`fecho/cloudscan.py` 的 `AGENTS["cursor"]["transcripts"]` 和 `SCANNABLE`；
`fecho/presets/local/fecho_local.py` 的 `TRANSCRIPTS`、`normalized_records`、`run_agent`、`probe`、`FIX_HINT`；
接入指南和 install.md 里「Cursor 只能随手记」的说法。

## ③ 日报里删掉的图不会被清理（2026-09-16 记）

日报里贴的图存在 `report_images` 表。改日报时把图删了、或者整份日报被重新生成覆盖，图本身还留在库里（历史版本可能还引用它）。
量不大时不用管；以后要清理，得先扫一遍所有日报和历史版本里还引用着哪些图。
