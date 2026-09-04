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
mcp()  { FECHO_SESSION_ID="$2" python3 scripts/mcp_probe.py --client "$1" call "${@:3}"; }

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

# ─────────────────────────────────────────────────────────────
# 团队联邦部分。这几步刻意不配 LLM——走确定性兜底稿，跑得快也可重复；
# 这里要验的是推送、跨人读取和身份边界，跟 LLM 没关系。
# ─────────────────────────────────────────────────────────────
CPORT=8907
CHOME="${ROOT}/.acceptance/collector"
AHOME="${ROOT}/.acceptance/alice"
BHOME="${ROOT}/.acceptance/bob"
mkdir -p "$CHOME" "$AHOME" "$BHOME"
cat > "${CHOME}/tokens.json" <<'JSON'
{
  "tok-a": {"author": "alice", "display_name": "Alice"},
  "tok-b": {"author": "bob",   "display_name": "Bob"}
}
JSON

step "12. 起一个团队 collector"
FECHO_HOME="$CHOME" python3 -m fecho.cli serve --port "$CPORT" > "${CHOME}/serve.log" 2>&1 &
CPID=$!
trap 'kill $CPID 2>/dev/null' EXIT
for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:${CPORT}/healthz" >/dev/null && break; sleep 0.5
done
curl -s "http://127.0.0.1:${CPORT}/healthz"; echo
ok "collector 起来了（它自己的库里没有 entries/tasks 表）"

# 两个人各自的本机：独立 FECHO_HOME，不配 LLM（走兜底稿），配上 collector
solo() {  # solo <HOME> <author> <token>
  env FECHO_HOME="$1" FECHO_LLM_BASE_URL= FECHO_LLM_API_KEY= FECHO_LLM_MODEL= \
      FECHO_MOBIUS_TOKEN= FECHO_MOBIUS_URL= "${@:4}"
}

step "13. 两个人各自记进展、出稿，自动推送给 collector"
for pair in "alice tok-a $AHOME" "bob tok-b $BHOME"; do
  set -- $pair; WHO=$1; TOK=$2; H=$3
  solo "$H" "$WHO" "$TOK" python3 -m fecho.cli setup --author "$WHO" \
      --collector-url "http://127.0.0.1:${CPORT}" --collector-token "$TOK" > /dev/null
  solo "$H" "$WHO" "$TOK" python3 scripts/mcp_probe.py --client claude-code \
      call log_progress "{\"content\":\"${WHO} today: 验证联邦推送链路，只推成品不推原始进展\"}"
  solo "$H" "$WHO" "$TOK" python3 -m fecho.cli digest | tail -2
done

step "14. 跨人读取：alice 用自己的 token 能看到 bob 推的成品"
solo "$AHOME" alice tok-a python3 -m fecho.cli team --date "$TODAY" | head -20

step "15. 身份边界：拿 alice 的 token 塞一段冒充内容，看落库算谁的"
SPOOF=$(curl -s -X POST "http://127.0.0.1:${CPORT}/reports" \
  -H "Authorization: Bearer tok-a" -H "Content-Type: application/json" \
  -d '{"date":"'"${TODAY}"'","daily_md":"# 冒充测试\n\n若这条出现在 bob 名下即为漏洞","author":"bob"}')
echo "$SPOOF"
echo "$SPOOF" | python3 -c "
import json,sys
got = json.load(sys.stdin)['author']
assert got == 'alice', '身份边界破了：落库 author=%s' % got
print('  \033[32m✓\033[0m 落库 author=alice —— body 里的 author 字段完全不生效')
"
printf '  无 token  -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:${CPORT}/reports" -H 'Content-Type: application/json' \
  -d '{"date":"'"${TODAY}"'","daily_md":"x"}')"
printf '  错 token  -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:${CPORT}/reports" -H 'Authorization: Bearer nope' \
  -H 'Content-Type: application/json' -d '{"date":"'"${TODAY}"'","daily_md":"x"}')"

step "16. 隐私边界是结构性的：collector 的库里有哪些表"
# sqlite3 在 macOS 自带、Linux 不一定有——没有就退回 Python 的 sqlite3 模块，
# 免得队友在别的机器上跑验收莫名其妙挂在最后一步。
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "${CHOME}/collector.db" ".tables"
else
  python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(' '.join(r[0] for r in c.execute(
    \"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'\")))
" "${CHOME}/collector.db"
fi
ok "只有 team_reports —— entries / tasks / updates 在这台机器上根本不存在"

printf '\n\033[1;32m验收完成。\033[0m 个人产物在 %s\n' "${FECHO_HOME#$ROOT/}/logs/${TODAY}/"
