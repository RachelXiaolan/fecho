#!/usr/bin/env bash
# Fecho 验收剧本。全程真机：真 Mobius、真 LLM、真 MCP stdio。
# 用独立 FECHO_HOME，不碰你日常的 ~/.fecho。
#   ./scripts/acceptance.sh
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
[ -f .env ] && set -a && . ./.env && set +a

export FECHO_HOME="${ROOT}/.acceptance"
rm -rf "$FECHO_HOME"; mkdir -p "$FECHO_HOME"
TODAY="$(date +%F)"

step() { printf '\n\033[1;36m━━━ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
mcp()  { SCRIBE_SESSION_ID="$2" FECHO_SESSION_ID="$2" python3 scripts/mcp_probe.py --client "$1" call "${@:3}"; }

step "0. 干净安装状态：fecho doctor"
python3 -m fecho.cli doctor
python3 tests/test_core.py 2>&1 | tail -3

step "1. MCP 工具清单（真 stdio 子进程）"
python3 scripts/mcp_probe.py --client claude-code list

step "2. 连 Mobius（走 token，跳过浏览器；生产环境走 mobius_login 会开浏览器）"
python3 scripts/mcp_probe.py --client claude-code call sync_issues '{}'
python3 -m fecho.cli doctor | sed -n '1,6p'

step "3. 一个对话里推进同一个任务，两条进展要自动接续"
mcp claude-code sess-A log_progress '{"content":"读完 AI-2541 的 PRD 重构说明，理解了任务模型要换掉"}'
mcp claude-code sess-A log_progress '{"content":"验收脚本重写完了，走单进程 CLI，不用先起服务"}'

step "4. 另一个对话，内容自动配到别的 Mobius issue（没点名 issue 号）"
mcp codex sess-B log_progress '{"content":"本地试了下 awesome-gpt-image-2，出图质量一般，不值得复用"}'

step "5. 完全不相关的话，应该新立自由任务"
mcp hermes sess-C log_progress '{"content":"帮同事看了下他那个爬虫为什么超时，是 DNS 解析卡住了"}'

step "6. 逐字重复提交：应该被挡，不产生新记录"
mcp claude-code sess-A log_progress '{"content":"验收脚本重写完了，走单进程 CLI，不用先起服务"}'
ok "重复提交被挡，任务数没有虚增"

step "7. 当前任务视图"
python3 scripts/mcp_probe.py --client claude-code call my_tasks '{}'

step "8. 日终整理 —— 真实调用 LLM"
time python3 -m fecho.cli digest

step "9. 幂等：同样的输入再跑一次"
python3 -m fecho.cli digest
ok "输入没变就不重复烧 token"

step "10. catch_up —— 开工时拉回昨天/今天的稿子和还挂着的任务"
python3 scripts/mcp_probe.py --client claude-code call catch_up "{\"date\":\"${TODAY}\"}"

step "11. 交付物 —— 人要读的文件"
for f in "${FECHO_HOME}/logs/${TODAY}"/*-日报.md "${FECHO_HOME}/logs/${TODAY}"/*-口播稿.md; do
  printf '\n\033[1;33m═══ %s\033[0m\n' "${f#$ROOT/}"; cat "$f"
done

printf '\n\033[1;32m验收完成。\033[0m 产物在 %s\n' "${FECHO_HOME#$ROOT/}/logs/${TODAY}/"
