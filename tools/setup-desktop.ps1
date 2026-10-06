<#
  给「朝夕桌面版」准备隔离环境：建 .venv 并安装 requirements-desktop.txt。

  源码直跑用，不打包。装好后双击仓库根目录的「朝夕桌面版.cmd」即可。
  重复运行是安全的：已有 .venv 会直接复用，依赖已装齐则秒退。
#>

$ErrorActionPreference = 'Stop'

$repo = Split-Path $PSScriptRoot -Parent
$venvDir = Join-Path $repo '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host '[朝夕] 创建虚拟环境 .venv ...'
    $launcher = Get-Command python -ErrorAction SilentlyContinue
    if (-not $launcher) {
        throw '没有找到 python 命令。请先装 Python 3.10 或更高版本，安装时勾选 Add Python to PATH。'
    }
    & $launcher.Source -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败（退出码 $LASTEXITCODE）" }
} else {
    Write-Host '[朝夕] 已有 .venv，直接复用。'
}

Write-Host '[朝夕] 安装桌面版依赖 ...'
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repo 'requirements-desktop.txt')
if ($LASTEXITCODE -ne 0) { throw "pip 安装失败（退出码 $LASTEXITCODE）" }

& $venvPython -c "import webview, PIL; print('[朝夕] pywebview 与 Pillow 就绪')"
Write-Host '[朝夕] 环境准备完成。'
