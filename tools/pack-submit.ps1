# 打包发布 zip：把仓库根（即本脚本所在目录的上一层）收进包里，产出四个：
#   ① 标准包            daylong-<日期>.zip
#                        （约 1 MB，需要本机 Python 3.10+）
#   ② Windows 免安装版  daylong-windows-x86_64-<日期>.zip
#                        （约 13 MB，自带 Windows 版 Python，开箱即用）
#   ③ Mac 免安装版      daylong-macos-<日期>.zip
#                        （约 20 MB，自带 Mac 双架构 Python，开箱即用）
#   ④ 全平台免安装版    daylong-all-platforms-<日期>.zip
#                        （约 34 MB，Windows + Mac 运行时都带）
# 可重跑：每次重建 staging，覆盖同一天的旧产物（文件名带日期，不同天互不覆盖）。
#
# 用法：
#   .\tools\pack-submit.ps1                          # 输出到 <仓库>\dist
#   .\tools\pack-submit.ps1 -OutDir D:\out
#   .\tools\pack-submit.ps1 -RuntimeDir <目录>       # 免安装版需要
#
# 关于 -RuntimeDir：免安装版要往包里的 python-runtime\ 叠 CPython，本仓库**不分发**
# 这些 tar（约 68 MB，且是官方原始发布物）。目录里应有两层：
#   <RuntimeDir>\mac-runtime\  cpython-*-aarch64-apple-darwin-*.tar.gz
#                              cpython-*-x86_64-apple-darwin-*.tar.gz
#                              prune.txt
#   <RuntimeDir>\win-runtime\  cpython-*-x86_64-pc-windows-msvc-*.tar.gz
#                              prune.txt
# 未指定时依次尝试 $env:ZHAOXI_RUNTIME_DIR 与 <仓库>\dist\runtime。
# 下载地址、三个 tar 的 SHA256 与裁剪说明见 THIRD_PARTY_NOTICES.md。
#
# 排除清单（每一项都有理由）：
#   .env          真实 API key，永不进发布包
#   data/         本机运行数据（日程/收藏/页面上填的 AI 设置），发布包应像新装的
#   .git/         git 历史，源码包不需要（GitHub 自己会给源码 zip）
#   .playwright-cli/  浏览器自动化缓存
#   __pycache__/  Python 字节码
#   screenshots/  探针截图（若存在）
#   dist/         上一次的产物
#   tools/ tests/ docs/  ACCEPTANCE.md  面向维护者而非使用者
#
# 为什么不用 Compress-Archive：Windows PowerShell 5.1 的它把条目名写成反斜杠，
# macOS 的「归档实用工具」解出来是一堆名字里带 \ 的平铺文件；它也不记录 Unix 权限位，
# 一键启动朝夕.command 到了 Mac 上会没有可执行权限。所以交给 zip_tree.py
# （标准库 zipfile：正斜杠路径、UTF-8 中文名、脚本 0755）。免安装版额外把
# mac-runtime 与 win-runtime 里的 tar.gz 按各自 prune.txt 裁剪后叠进 python-runtime\，
# 并校验 bin/python3.12 的 Mach-O 头与 python.exe 的 PE 头（x86_64）。
param(
    [string]$OutDir = '',
    [string]$RuntimeDir = ''
)
$ErrorActionPreference = "Stop"

$repo = Split-Path $PSScriptRoot                 # 仓库根 = tools\ 的上一层
if (-not $OutDir) { $OutDir = Join-Path $repo "dist" }
$stage = Join-Path $env:TEMP "daylong"
$stamp = Get-Date -Format "yyyy-MM-dd"
$zipStd = Join-Path $OutDir "daylong-$stamp.zip"
$zipWin = Join-Path $OutDir "daylong-windows-x86_64-$stamp.zip"
$zipMac = Join-Path $OutDir "daylong-macos-$stamp.zip"
$zipAll = Join-Path $OutDir "daylong-all-platforms-$stamp.zip"
$top = "daylong"  # zip 内的顶层目录名（= staging 目录名，zip_tree.py 按源目录名取）

function Get-ZipNames([string]$Path) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $a = [System.IO.Compression.ZipFile]::OpenRead($Path)
  $n = @($a.Entries | ForEach-Object { $_.FullName })
  $a.Dispose()
  return $n
}

function Test-ReleaseZip([string]$Path, [string]$Label, [string[]]$MustContain) {
  $names = Get-ZipNames $Path
  $bs = @($names | Where-Object { $_.Contains('\') })
  if ($bs.Count -gt 0) { throw "$Label 内有反斜杠路径（Mac 解压会散成平铺文件）：$($bs[0])" }
  # __pycache__ 只算「本机字节码污染」：出现在项目源码里就报错；
  # python-runtime 下的是官方 tar 自带内容，不算。
  $bad = @($names | Where-Object {
      $_ -match '(^|/)\.env$' -or $_ -match '(^|/)(tmp|data|dist|\.git|\.playwright-cli|screenshots)/' -or
      $_ -match 'ai_settings\.json$' -or (($_ -match '(^|/)__pycache__/') -and ($_ -notmatch '/python-runtime/'))
    })
  if ($bad.Count -gt 0) { throw "$Label 内有敏感条目：$($bad -join '、')" }
  $miss = @($MustContain | Where-Object { $m = $_; -not ($names -contains $m) })
  if ($miss.Count -gt 0) { throw "$Label 缺少必需条目：$($miss -join '、')" }
  return $names
}

# 在 $RuntimeDir 下按文件名模式找 tar，避免版本号升级后要改脚本
function Find-RuntimeTar([string]$SubDir, [string]$Pattern) {
  $dir = Join-Path $RuntimeDir $SubDir
  if (-not (Test-Path $dir)) { return $null }
  $hit = @(Get-ChildItem -Path $dir -Filter $Pattern -File)
  if ($hit.Count -ne 1) { return $null }
  return $hit[0].FullName
}

Write-Host "1/6 解析运行时目录..."
if (-not $RuntimeDir) {
    foreach ($cand in @($env:ZHAOXI_RUNTIME_DIR, (Join-Path $repo "dist\runtime"))) {
        if ($cand -and (Test-Path $cand)) { $RuntimeDir = $cand; break }
    }
}
if ($RuntimeDir -and (Test-Path $RuntimeDir)) {
    Write-Host "    运行时目录：$RuntimeDir"
} else {
    Write-Host "    未找到运行时目录——将只产出标准包（免安装版会被跳过）"
    $RuntimeDir = $null
}

Write-Host "2/6 重建 staging..."
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "3/6 复制并排除敏感/临时内容..."
robocopy $repo $stage /E /XD .git data dist __pycache__ .playwright-cli screenshots tools tests docs `
  /XF .env .gitignore .gitattributes ACCEPTANCE.md /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }

Write-Host "4/6 安全检查（敏感/临时内容必须为 0）..."
$leak = @()
if (Get-ChildItem $stage -Recurse -Force -File | Where-Object Name -eq ".env") { $leak += ".env 文件" }
foreach ($dir in @("data", ".git", ".playwright-cli", "__pycache__", "screenshots", "dist")) {
  if (Get-ChildItem $stage -Recurse -Force -Directory | Where-Object Name -eq $dir) { $leak += "$dir 目录" }
}
if ($leak.Count -gt 0) { throw "发现不该进包的内容：$($leak -join '、')" }

# .env 里「像密钥」的取值（≥20 字符且不含 : / 空白）当"指纹"，扫 staging 里的文本文件
# 有没有谁不小心抄进去。BASE_URL、端口、路径这类文档里本来就会正常出现的取值不算指纹。
# 命中只报文件名，绝不打印密钥值本身。
$envFile = Join-Path $repo ".env"
if (Test-Path $envFile) {
  $needles = @(Get-Content $envFile -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+?)\s*$') { $Matches[1].Trim('"').Trim("'") }
  } | Where-Object { $_.Length -ge 20 -and $_ -notmatch '[:/\s]' })
  foreach ($f in Get-ChildItem $stage -Recurse -Force -File) {
    if ($f.Length -gt 4MB) { continue }
    $text = Get-Content $f.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { continue }
    foreach ($n in $needles) {
      if ($text.Contains($n)) { throw "密钥值出现在 $($f.FullName -replace [regex]::Escape($stage), '')，不能进发布包" }
    }
  }
  Write-Host "    密钥指纹 $($needles.Count) 个，已扫描 staging（未打印任何值）"
}

Write-Host "5/6 打 zip（正斜杠路径 / UTF-8 中文名 / 脚本可执行）..."
$zt = Join-Path $PSScriptRoot "zip_tree.py"
$tmpStd = "$zipStd.new"
Remove-Item $tmpStd -Force -ErrorAction SilentlyContinue
python $zt $stage $tmpStd
if ($LASTEXITCODE -ne 0) { throw "zip_tree.py（标准包）失败，退出码 $LASTEXITCODE" }

$mustStd = @(
  "$top/一键启动朝夕.command",
  "$top/一键启动朝夕.bat",
  "$top/server.py",
  "$top/static/index.html",
  "$top/README.md",
  "$top/LICENSE",
  "$top/THIRD_PARTY_NOTICES.md"
)
$built = @{ "标准包" = @{ Tmp = $tmpStd; Dst = $zipStd; Must = $mustStd } }

if ($RuntimeDir) {
    $tarMac1 = Find-RuntimeTar "mac-runtime" "cpython-*-aarch64-apple-darwin-*.tar.gz"
    $tarMac2 = Find-RuntimeTar "mac-runtime" "cpython-*-x86_64-apple-darwin-*.tar.gz"
    $tarWin  = Find-RuntimeTar "win-runtime" "cpython-*-x86_64-pc-windows-msvc-*.tar.gz"
    $pruneMac = Join-Path $RuntimeDir "mac-runtime\prune.txt"
    $pruneWin = Join-Path $RuntimeDir "win-runtime\prune.txt"
    foreach ($p in @($tarMac1, $tarMac2, $tarWin, $pruneMac, $pruneWin)) {
        if (-not $p -or -not (Test-Path $p)) { throw "运行时目录缺少文件：$p（清单见 THIRD_PARTY_NOTICES.md）" }
    }
    $argsMac = @("--add-tar", "$tarMac1=python-runtime/macos-arm64",
                 "--add-tar", "$tarMac2=python-runtime/macos-x86_64",
                 "--prune", $pruneMac)
    $argsWin = @("--add-tar", "$tarWin=python-runtime/windows-x86_64",
                 "--prune", $pruneWin)

    $tmpWin = "$zipWin.new"; $tmpMac = "$zipMac.new"; $tmpAll = "$zipAll.new"
    Remove-Item $tmpWin, $tmpMac, $tmpAll -Force -ErrorAction SilentlyContinue

    python $zt $stage $tmpWin @argsWin
    if ($LASTEXITCODE -ne 0) { throw "zip_tree.py（Windows 免安装版）失败，退出码 $LASTEXITCODE" }
    python $zt $stage $tmpMac @argsMac
    if ($LASTEXITCODE -ne 0) { throw "zip_tree.py（Mac 免安装版）失败，退出码 $LASTEXITCODE" }
    python $zt $stage $tmpAll @argsMac @argsWin
    if ($LASTEXITCODE -ne 0) { throw "zip_tree.py（全平台免安装版）失败，退出码 $LASTEXITCODE" }

    $mustWin = $mustStd + @("$top/python-runtime/windows-x86_64/python.exe",
                            "$top/python-runtime/windows-x86_64/Lib/encodings/utf_8.py")
    $mustMac = $mustStd + @("$top/python-runtime/macos-arm64/bin/python3.12",
                            "$top/python-runtime/macos-x86_64/bin/python3.12",
                            "$top/python-runtime/macos-arm64/lib/python3.12/encodings/utf_8.py")
    $mustAll = $mustWin + @("$top/python-runtime/macos-arm64/bin/python3.12",
                            "$top/python-runtime/macos-x86_64/bin/python3.12")
    $built["Windows 免安装版"] = @{ Tmp = $tmpWin; Dst = $zipWin; Must = $mustWin }
    $built["Mac 免安装版"]     = @{ Tmp = $tmpMac; Dst = $zipMac; Must = $mustMac }
    $built["全平台免安装版"]   = @{ Tmp = $tmpAll; Dst = $zipAll; Must = $mustAll }
}

Write-Host "6/6 回读验证并落盘..."
foreach ($label in $built.Keys) {
    $b = $built[$label]
    $b["Names"] = @(Test-ReleaseZip $b.Tmp $label $b.Must)
}
foreach ($label in $built.Keys) {
    Move-Item -Force $built[$label].Tmp $built[$label].Dst
}
Write-Host ""
Write-Host "完成（输出目录：$OutDir）："
foreach ($label in @("标准包", "Windows 免安装版", "Mac 免安装版", "全平台免安装版")) {
    if (-not $built.ContainsKey($label)) { continue }
    $b = $built[$label]
    $mb = [Math]::Round((Get-Item $b.Dst).Length / 1MB, 2)
    Write-Host ("  {0,-16} {1}" -f $label, $b.Dst)
    Write-Host ("                   {0} 个条目，{1} MB" -f $b.Names.Count, $mb)
}
Write-Host ""
Write-Host "标准包顶层："
$built["标准包"].Names | ForEach-Object { ($_ -split '/')[1] } | Sort-Object -Unique |
    Where-Object { $_ } | ForEach-Object { Write-Host "  $_" }
Remove-Item $stage -Recurse -Force
