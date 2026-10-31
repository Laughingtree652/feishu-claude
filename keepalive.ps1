# bot.py 进程保活：进程不在（崩溃/看门狗自杀/断网后）就拉起
# 由 Windows 计划任务 FeishuClaudeKeepAlive 每 5 分钟调用一次。
# 卸载：schtasks /delete /tn FeishuClaudeKeepAlive /f
$py = "D:\work\feishu-claude\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { exit 1 }
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" }
if (-not $running) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "D:\work\feishu-claude\restart.ps1"
}
