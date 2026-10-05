# Launch any GUI executable, bring its window to the front, screenshot it, then close it.
# Dev-only helper (ASCII only so Windows PowerShell 5.1 parses it regardless of encoding).
param(
  [Parameter(Mandatory=$true)][string]$Exe,
  [string]$Args   = "",
  [string]$Out    = (Join-Path (Split-Path -Parent $PSScriptRoot) "docs\screenshot.png"),
  [int]$WaitSeconds = 35,
  [switch]$ViaExplorer,
  [switch]$ViaTask
)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinApi {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
}
"@

New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null

$before = @{}
foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) {
  if ($p.MainWindowHandle -ne 0 -and $p.MainWindowTitle -ne "") { $before[$p.Id] = $true }
}
Write-Host "windows before: $($before.Count)"

# Shell dialogs (Explorer's "open file" prompt etc.) must never be mistaken for our app.
$shellNoise = @("explorer", "rundll32", "dllhost", "ApplicationFrameHost", "ShellExperienceHost")
$wantName = [System.IO.Path]::GetFileNameWithoutExtension($Exe)
Write-Host "expecting process: $wantName"

$taskName = "dshmig-gui-test"
if ($ViaTask) {
  # Run through the Task Scheduler: the process gets a normal user token and is not
  # a DSH child, which is the closest equivalent to the user double-clicking it.
  Write-Host "launching via scheduled task"
  & schtasks /delete /tn $taskName /f 2>$null | Out-Null
  & schtasks /create /tn $taskName /tr "`"$Exe`"" /sc once /st 23:59 /f | Out-Null
  & schtasks /run /tn $taskName | Out-Null
  $proc = $null
} elseif ($ViaExplorer) {
  # Hand the launch to Explorer so the process gets the normal user token
  # (equivalent to the user double-clicking it) instead of the sandboxed shell.
  Write-Host "launching via explorer"
  Start-Process -FilePath "explorer.exe" -ArgumentList "`"$Exe`""
  $proc = $null
} elseif ($Args -ne "") {
  $proc = Start-Process -FilePath $Exe -ArgumentList $Args -PassThru
} else {
  $proc = Start-Process -FilePath $Exe -PassThru
}
Write-Host "started pid=$(if ($proc) { $proc.Id } else { 'via-explorer' })"

$target = $null
for ($i = 0; $i -lt $WaitSeconds; $i++) {
  Start-Sleep -Seconds 1
  # 首选：进程名与目标 exe 同名（最可靠，避免抓到别的程序新弹出的窗口）
  $named = Get-Process -Name $wantName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne "" } |
    Select-Object -First 1
  if ($named) { $target = $named; break }
  # 退路：本次启动后新出现的、非 shell 噪声的窗口
  foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) {
    if ($p.MainWindowHandle -ne 0 -and $p.MainWindowTitle -ne "" -and -not $before.ContainsKey($p.Id) -and
        $shellNoise -notcontains $p.ProcessName) {
      $target = $p
      break
    }
  }
  if ($target) { break }
}
if (-not $target) {
  Write-Host "NO NEW WINDOW after ${WaitSeconds}s"
  if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
  exit 1
}
Write-Host "window pid=$($target.Id) name=$($target.ProcessName) title=$($target.MainWindowTitle)"

$hwnd = $target.MainWindowHandle
[WinApi]::ShowWindow($hwnd, 5) | Out-Null
[WinApi]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 1800

$r = New-Object WinApi+RECT
[WinApi]::GetWindowRect($hwnd, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$h = $r.Bottom - $r.Top
Write-Host "rect=$($r.Left),$($r.Top) ${w}x${h}"
if ($w -le 0 -or $h -le 0) { Write-Host "BAD RECT"; exit 1 }

# PrintWindow renders the window itself, so overlapping dialogs cannot pollute the shot.
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$printed = [WinApi]::PrintWindow($hwnd, $hdc, 2)
$g.ReleaseHdc($hdc)
if (-not $printed) {
  Write-Host "PrintWindow failed, falling back to screen copy"
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
}
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
Write-Host "saved $Out"

Start-Sleep -Milliseconds 300
& taskkill /PID $target.Id /T /F 2>$null | Out-Null
if ($ViaTask) { & schtasks /delete /tn $taskName /f 2>$null | Out-Null }
Write-Host "closed"
