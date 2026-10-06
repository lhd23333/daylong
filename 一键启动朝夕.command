#!/bin/bash
# 朝夕 · 陪你过完这一天 —— macOS / Linux 一键启动（对应 Windows 的「一键启动朝夕.bat」）
#
# 用法：在 Finder 里双击本文件，会打开「终端」，启动服务并自动打开浏览器。
#   · 「Mac 免安装版」自带 Python（python-runtime 文件夹），什么都不用装；
#   · 标准包没有这个文件夹，需要本机有 Python 3.10+，找不到时会告诉你去哪装。
# 第一次被系统拦下时怎么放行：见同目录的「Mac用户先看这里.txt」。
# 换端口：ZHAOXI_PORT=8080 ./一键启动朝夕.command

pause() {
  echo
  read -r -p "按回车键关闭窗口..." _
}

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1"
  fi
}

# /api/health 答得上来，说明"朝夕"已经在这个端口上跑着了。
is_up() {
  command -v curl >/dev/null 2>&1 || return 1
  case "$(curl -fs --noproxy '*' --max-time 1 "$URL/api/health" 2>/dev/null)" in
    *audio_generation*) return 0 ;;
  esac
  return 1
}

# 等服务真的起来再开浏览器（最多 20 秒）；没有 curl 时退回固定等 2 秒。
open_when_ready() {
  local i=0
  if command -v curl >/dev/null 2>&1; then
    while [ "$i" -lt 40 ]; do
      if is_up; then
        open_url "$URL"
        return 0
      fi
      sleep 0.5
      i=$((i + 1))
    done
  else
    sleep 2
    open_url "$URL"
  fi
}

OS="$(uname -s)"
ROOT="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$ROOT" || exit 1

# 不依赖中文目录名：哪个子目录里有 server.py 就进哪个。
for d in "$ROOT"/*/; do
  if [ -f "${d}server.py" ]; then
    cd "$d" || exit 1
    break
  fi
done

if [ ! -f server.py ]; then
  echo "找不到 server.py。"
  echo "请确认本文件与 server.py 在同一层。"
  pause
  exit 1
fi

PORT="${ZHAOXI_PORT:-8000}"
URL="http://127.0.0.1:${PORT}"

# 已经有一份在跑（比如重复双击）：不再起第二份，直接把网页打开。
if is_up; then
  echo "朝夕已经在运行了（${URL}），这就为你打开网页。"
  open_url "$URL" >/dev/null 2>&1
  sleep 1
  exit 0
fi

# Finder 双击启动的终端 PATH 很短：补上 Homebrew / python.org 的常见位置，放在最后，不抢用户自己的优先级。
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin"
export PATH

# 服务端只用标准库：别让用户环境里的 PYTHONHOME / PYTHONPATH / 用户级 site-packages 干扰解释器。
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1

# 从网上下载、再用「归档实用工具」解压出来的文件带着"隔离"标记，系统会因此拦住里面自带的 Python。
# 这里把本文件夹里的标记统一摘掉（只动这个文件夹）。失败也无妨。
# xattr 在 macOS 12.3～13.1 是靠"命令行工具"里的 Python 跑的脚本，没装命令行工具时碰它会弹出安装框，这种情况跳过。
clear_quarantine() {
  [ "$OS" = "Darwin" ] || return 0
  command -v xattr >/dev/null 2>&1 || return 0
  if [ "$(head -c 2 /usr/bin/xattr 2>/dev/null)" = "#!" ] \
     && [ ! -x /usr/bin/python ] && ! xcode-select -p >/dev/null 2>&1; then
    return 0
  fi
  xattr -dr com.apple.quarantine "$ROOT" >/dev/null 2>&1 || true
}
clear_quarantine

# 找 Python 3.10+：带版本号的优先，最后才是 python3 / python。
# macOS 没装「命令行工具」时，/usr/bin/python3 只是个引导去装 Xcode 工具的桩，
# 碰它会弹出安装对话框，所以这种情况直接跳过。
usable_python() {
  local p
  p="$(command -v "$1" 2>/dev/null)" || return 1
  if [ "$p" = "/usr/bin/python3" ] && [ "$OS" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PY=""
PY_NOTE=""

# 1) 免安装版自带的 Python（python-runtime/macos-<芯片>/）。
#    试运行一次"能不能把服务端代码导进来"，不行就当没有，退回系统里的。
if [ "$OS" = "Darwin" ]; then
  BUNDLED="$ROOT/python-runtime/macos-$(uname -m)/bin/python3.12"
  if [ -f "$BUNDLED" ]; then
    [ -x "$BUNDLED" ] || chmod +x "$BUNDLED" 2>/dev/null
    if "$BUNDLED" -c 'import sys; sys.exit(1) if sys.version_info < (3, 10) else None; sys.path.insert(0, "."); import server' >/dev/null 2>&1; then
      PY="$BUNDLED"
      PY_NOTE="随包自带"
    else
      echo "（随包自带的 Python 没能启动，改用本机已装的 Python。）"
    fi
  fi
fi

# 2) 本机已装的 Python。
if [ -z "$PY" ]; then
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if usable_python "$c"; then
      PY="$(command -v "$c")"
      PY_NOTE="本机已装"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  if [ "$OS" = "Darwin" ]; then
    DOWNLOAD_URL="https://www.python.org/downloads/macos/"
  else
    DOWNLOAD_URL="https://www.python.org/downloads/"
  fi
  echo "没有找到 Python 3.10 或更高版本。"
  echo "（系统自带的 python3 版本可能低于 3.10，不够用。）"
  echo
  echo "两个办法，选一个："
  echo "  1. 改用「Mac 免安装版」压缩包：自带 Python，什么都不用装"
  echo "  2. 装 Python：下载页 ${DOWNLOAD_URL} 已经帮你打开了，"
  echo "     下载安装包一路点“继续”就行；或者用 Homebrew：brew install python"
  echo "装好之后重新双击本文件。"
  open_url "$DOWNLOAD_URL" >/dev/null 2>&1
  pause
  exit 1
fi

PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"

echo
echo "  朝夕 · 陪你过完这一天"
echo
echo "  地址:   ${URL}"
echo "  Python: ${PYVER}（${PY_NOTE}）"
echo "  停止:   在这个窗口按 Ctrl+C"
echo

# 后台等服务起来再开浏览器（macOS 用 open，Linux 用 xdg-open）。
( open_when_ready ) >/dev/null 2>&1 &

"$PY" server.py --host 127.0.0.1 --port "$PORT"
STATUS=$?

echo
# 0 是正常退出，130 是 Ctrl+C。
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 130 ]; then
  echo "服务没有正常启动（退出码 ${STATUS}）。"
  echo "最常见的原因是端口 ${PORT} 被别的程序占着。换个端口再试："
  echo "  打开「终端」，输入  ZHAOXI_PORT=8080 bash  和一个空格，"
  echo "  再把本文件拖进终端窗口，按回车。"
else
  echo "服务已停止。"
fi
pause
exit $STATUS
