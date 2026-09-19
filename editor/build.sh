#!/bin/sh
# 把 entry.js 打包成页面能直接 <script> 引的一个文件
set -e
cd "$(dirname "$0")"
OUT=../fecho/presets/vendor/cm6/editor.min.js
npx esbuild entry.js --bundle --minify --format=iife --outfile="$OUT" --log-level=warning
echo "产物：$OUT  ($(wc -c < "$OUT" | tr -d ' ') 字节)"
