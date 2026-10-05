# 取消"等待 DSH 退出后自动迁移"的等待进程。
# 注意：venv 的 pythonw.exe 会派生一个真正跑代码的子进程，两个都要结束。
$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*auto_migrate*' } |
  ForEach-Object {
    try {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
      Write-Host "已结束 PID $($_.ProcessId)"
      $killed++
    } catch {
      Write-Host "结束 PID $($_.ProcessId) 失败: $($_.Exception.Message)"
    }
  }
if ($killed -eq 0) { Write-Host "没有找到等待进程（可能已经执行完或已被取消）" }
else { Write-Host "已取消自动迁移，DSH 数据不会被改动。" }
