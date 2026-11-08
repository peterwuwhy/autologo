#!/bin/bash
# 發布新版（在 Mac 的「終端機」執行）：
#   ./release.sh 1.1.0 "修正 Logo 位置；新增 xx 功能"
# 會修改 version.py、存檔到 GitHub，GitHub 約 5 分鐘後打包好 exe 並發布。
# 工廠電腦在「診斷」分頁按「檢查更新」→「立即更新」即可。
set -e
cd "$(dirname "$0")"
ver="$1"
notes="$2"
if [[ ! "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || [ -z "$notes" ]; then
  echo "用法：./release.sh 1.1.0 \"這次更新的內容\""
  exit 1
fi
current=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' version.py)
if [ "$ver" = "$current" ] || [ "$(printf '%s\n%s\n' "$current" "$ver" | sort -V | tail -1)" != "$ver" ]; then
  echo "新版本號 $ver 必須大於目前的 $current"
  exit 1
fi
if git rev-parse -q --verify "refs/tags/v$ver" >/dev/null; then
  echo "v$ver 已經發布過了"
  exit 1
fi
sed -i '' "s/^VERSION = .*/VERSION = \"$ver\"/" version.py
git add -A
git commit -q -m "v$ver：$notes"
git tag -a "v$ver" -m "$notes"
git push -q origin HEAD
git push -q origin "v$ver"
repo=$(sed -n 's/^GITHUB_REPO = "\(.*\)"/\1/p' version.py)
echo "已送出 v$ver。約 5 分鐘後打包完成：https://github.com/$repo/actions"
echo "完成後工廠電腦按「檢查更新」就會看到新版。"
