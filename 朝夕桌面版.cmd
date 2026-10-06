@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ==============================================================
rem 朝夕 - 桌面窗口版（源码直跑）
rem
rem 跑的就是这个目录里的最新代码：改 *.py 重启生效，改 static/ 关掉
rem 窗口重开生效（窗口里按 F5 也行）。不需要打包成 exe。
rem
rem 首次运行会自动建 .venv 并安装 pywebview（需要联网，约 1-2 分钟）。
rem 想要零依赖、开浏览器的版本，双击「一键启动朝夕.bat」。
rem ==============================================================

cd /d "%~dp0"

if not exist "desktop.py" (
  echo 找不到 desktop.py。
  echo 请确认本文件与 desktop.py 在同一层。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo.
  echo   首次运行：正在准备桌面版环境，需要联网，约 1-2 分钟...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\setup-desktop.ps1"
  if errorlevel 1 (
    echo.
    echo   环境准备失败。把上面的报错原样发给维护者即可。
    pause
    exit /b 1
  )
)

rem pythonw 无控制台，启动后本窗口立即退出；出错由桌面版自己弹窗提示。
start "" ".venv\Scripts\pythonw.exe" "desktop.py"
exit /b 0
