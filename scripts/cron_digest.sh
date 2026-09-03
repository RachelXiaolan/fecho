#!/usr/bin/env bash
# 日终整理。crontab 示例：
#   0  9 * * 1-5 fecho sync                      # 早上刷新 issue 缓存
#   30 19 * * 1-5 /path/to/scripts/cron_digest.sh # 下班前出稿
set -euo pipefail
exec fecho digest ${1:+--date "$1"}
