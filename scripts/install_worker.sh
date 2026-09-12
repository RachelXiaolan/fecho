#!/usr/bin/env bash
# 在 lu2 上装 Fecho 的后台程序（出日报的慢活跑在这里）。
#
#   bash install_worker.sh            装好并启动
#   bash install_worker.sh --update   拉最新代码重启（以后改了代码就跑这个）
#
# 装之前先准备好 /etc/fecho/worker.env，见 DEPLOY.md。
set -euo pipefail

REPO=${FECHO_REPO:-https://github.com/RachelXiaolan/fecho}
BRANCH=${FECHO_BRANCH:-cloud}
DIR=${FECHO_DIR:-/opt/fecho}
ENV_FILE=${FECHO_ENV_FILE:-/etc/fecho/worker.env}
SERVICE=fecho-worker

need_sudo() { [ "$(id -u)" = 0 ] && echo "" || echo "sudo"; }
S=$(need_sudo)

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE。先照 DEPLOY.md 建好它（里面有数据库地址、加密密钥、LLM key）。" >&2
  exit 1
fi
# 目录和文件权限：这个文件里有密钥，只给 root 读
$S install -d -m 700 "$(dirname "$ENV_FILE")"
$S chmod 600 "$ENV_FILE"

echo "==> 代码"
if [ -d "$DIR/.git" ]; then
  $S git -C "$DIR" fetch --quiet origin "$BRANCH"
  $S git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
else
  $S install -d -m 755 "$(dirname "$DIR")"
  $S git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi
echo "    $(cd "$DIR" && git log --oneline -1)"

echo "==> 依赖"
[ -d "$DIR/venv" ] || $S python3 -m venv "$DIR/venv"
$S "$DIR/venv/bin/pip" install -q --upgrade pip
$S "$DIR/venv/bin/pip" install -q "$DIR[cloud]"
echo "    fecho $($DIR/venv/bin/fecho --version)"

echo "==> 开机自启"
$S tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Fecho worker（到点出日报、处理排队的任务）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/venv/bin/fecho worker
Restart=always
RestartSec=10
User=root
# 崩了就重启，但别无限快速重启刷日志
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
UNIT
$S systemctl daemon-reload
$S systemctl enable --quiet $SERVICE
$S systemctl restart $SERVICE
sleep 3

echo "==> 状态"
$S systemctl is-active $SERVICE | sed 's/^/    /'
$S journalctl -u $SERVICE -n 8 --no-pager -o cat | sed 's/^/    /'
echo
echo "看日志：sudo journalctl -u $SERVICE -f"
