#!/usr/bin/env bash
# Fecho 日终自动化。装进 crontab：
#   0  9 * * 1-5  $HOME/.fecho/venv/bin/fecho sync        # 早上刷新 Mobius issue 缓存
#   0 21 * * *    /path/to/scripts/cron_daily.sh           # 晚上扫会话 + 出稿
#
# cron 的 PATH 很干净，所以一律用绝对路径。
set -uo pipefail
FECHO="${FECHO_BIN:-$HOME/.fecho/venv/bin/fecho}"
LOG="${FECHO_HOME:-$HOME/.fecho}/cron.log"

{
  echo "===== $(date '+%F %T %Z') ====="
  # 扫会话记录，把「做成了什么」自动记成进展。
  # --days 2 是为了漏跑一天也能补上；水位线保证不会重复记。
  "$FECHO" scan --days 2
  # 按任务整理成日报 + 口播稿。输入没变时跳过，不重复烧 token。
  "$FECHO" digest
} >> "$LOG" 2>&1
