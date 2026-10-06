@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem 可选：先在 cmd 里 set ZHAOXI_PORT=8080 再双击本文件，可以换端口。
if not defined ZHAOXI_PORT set "ZHAOXI_PORT=8000"

rem 不依赖中文代码页来找源码目录：哪个子目录里有 server.py 就进哪个。
cd /d "%~dp0"
for /d %%D in (*) do if exist "%%D\server.py" cd /d "%%D"

if not exist "server.py" goto no_project

rem 已经在运行就直接开浏览器（curl 是 Win10 1803+ 自带的，没有就跳过检测）。
where curl >nul 2>&1
if errorlevel 1 goto check_bundled
curl -s -m 2 http://127.0.0.1:%ZHAOXI_PORT%/api/health 2>nul | findstr /c:"audio_generation" >nul 2>&1
if not errorlevel 1 goto already_running

:check_bundled
rem 免安装版自带运行时：优先用它，坏了再退回本机 Python。
set "BUNDLED=%~dp0python-runtime\windows-x86_64\python.exe"
if exist "%BUNDLED%" (
  "%BUNDLED%" -c "import sys; sys.exit(1) if sys.version_info < (3, 10) else None; import server" >nul 2>&1
  if not errorlevel 1 (
    call :banner 随包自带
    "%BUNDLED%" server.py --host 127.0.0.1 --port %ZHAOXI_PORT%
    goto stopped
  )
  echo 随包自带的 Python 没能启动，改用本机已装的 Python。
)

where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if not errorlevel 1 goto use_python
goto no_python

:use_py
call :banner 本机已装
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process http://127.0.0.1:%ZHAOXI_PORT%"
py -3 server.py --host 127.0.0.1 --port %ZHAOXI_PORT%
goto stopped

:use_python
call :banner 本机已装
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process http://127.0.0.1:%ZHAOXI_PORT%"
python server.py --host 127.0.0.1 --port %ZHAOXI_PORT%
goto stopped

:banner
echo.
echo   朝夕 · 陪你过完这一天
echo.
echo   地址: http://127.0.0.1:%ZHAOXI_PORT%
echo   停止: 在这个窗口按 Ctrl+C
echo   Python: %~1
echo.
exit /b 0

:already_running
echo 朝夕已经在运行了，直接打开浏览器。
start "" http://127.0.0.1:%ZHAOXI_PORT%
ping -n 3 127.0.0.1 >nul 2>&1
exit /b 0

:no_project
echo 找不到 server.py。
echo 请确认本文件与 server.py 在同一层。
pause
exit /b 1

:no_python
echo 没有找到可用的 Python。
echo 装好 Python 3.10 或更高版本后重新双击本文件（安装时勾选 Add Python to PATH）。
echo 如果你用的是免安装版，说明随包的运行时没能启动；把本窗口里的文字复制出来，附到 Issue 里。
pause
exit /b 1

:stopped
echo.
echo 服务已停止。
pause
exit /b %ERRORLEVEL%
