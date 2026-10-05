# 打包 GUI 程序并安装到软件根目录（开发用脚本）
#
#   源码：<root>\src\          （本脚本在 <root>\src\tools\ 下）
#   安装：<root>\              （exe + _internal + 使用说明.txt）
#
# 采用 --onedir：单文件模式需要解压到 %TEMP%\_MEIxxxx，而本机 %TEMP% 指向
# C:\NewCachePath，引导程序在那里创建临时目录会失败（--runtime-tmpdir 也无效）。
# onedir 不做任何解压，启动也更快。
#
# 另：不要在 DSH 工作区目录里打包 —— 那里带 Windows「低完整性」标签，
# 产物会继承该标签，运行时写不了 .dsh。
$ErrorActionPreference = "Stop"
$srcRoot = Split-Path -Parent $PSScriptRoot
$installRoot = Split-Path -Parent $srcRoot
Set-Location $srcRoot

$py = Join-Path $srcRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }

$name = "DSH工作区迁移器"
$icon = Join-Path $srcRoot "dshmigrator\app.ico"
$distTmp = Join-Path $srcRoot "build\dist-tmp"
Write-Host "Building $name ..."
Write-Host "  src     : $srcRoot"
Write-Host "  install : $installRoot"

& $py -m PyInstaller `
  --noconfirm --clean --onedir --windowed `
  --name $name `
  --icon $icon `
  --add-data "$icon;dshmigrator" `
  --exclude-module numpy --exclude-module pandas --exclude-module PIL `
  --exclude-module unittest --exclude-module pydoc --exclude-module doctest `
  --exclude-module sqlite3 --exclude-module multiprocessing `
  --distpath $distTmp --workpath (Join-Path $srcRoot "build\work") --specpath (Join-Path $srcRoot "build") `
  "run.py"

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }

$built = Join-Path $distTmp $name
if (-not (Test-Path (Join-Path $built "$name.exe"))) { throw "expected output missing: $built" }

Write-Host "Installing to $installRoot ..."
New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
foreach ($item in Get-ChildItem $built -Force) {
  $dest = Join-Path $installRoot $item.Name
  if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
  Copy-Item $item.FullName $dest -Recurse -Force
}
Remove-Item $distTmp -Recurse -Force -ErrorAction SilentlyContinue

$exe = Join-Path $installRoot "$name.exe"
if (-not (Test-Path $exe)) { throw "install failed: $exe" }
$size = [math]::Round((Get-ChildItem $installRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "OK -> $exe ($size MB)"