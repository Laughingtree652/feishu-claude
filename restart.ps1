# 一键重启飞书机器人：杀掉现有实例，再启动一个新的独立窗口
# 用法（PowerShell 里）：  powershell -ExecutionPolicy Bypass -File D:\work\feishu-claude\restart.ps1
$ErrorActionPreference = "SilentlyContinue"
$base = "D:\work\feishu-claude"
$py = "$base\.venv\Scripts\python.exe"

# 停掉现有的 bot.py 实例（按命令行匹配，避免误杀其他 python）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" } |
    ForEach-Object {
        Write-Host "停止旧实例 PID=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Start-Sleep -Seconds 2

Write-Host "启动机器人…（新窗口，保持开着）"
Start-Process -FilePath $py -ArgumentList "$base\bot.py" -WorkingDirectory $base
Write-Host "完成。看到 ✅ 飞书机器人已启动 即为正常。"
