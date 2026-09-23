#!/bin/sh
# 把 entry.js 打包成页面能直接 <script> 引的一个文件
set -e
cd "$(dirname "$0")"
OUT=../fecho/presets/vendor/cm6/editor.min.js
npx esbuild entry.js --bundle --minify --format=iife --outfile="$OUT" --log-level=warning
echo "产物：$OUT  ($(wc -c < "$OUT" | tr -d ' ') 字节)"

# 日报截图用的 html-to-image
SNAP=../fecho/presets/vendor/snapshot/snapshot.min.js
npx esbuild snapshot.js --bundle --minify --format=iife --outfile="$SNAP" --log-level=warning
echo "产物：$SNAP  ($(wc -c < "$SNAP" | tr -d ' ') 字节)"
