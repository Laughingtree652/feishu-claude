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

Write-Host "启动机器人…（隐藏窗口，输出落 bot_console.log）"
# 隐藏 cmd 壳运行 python 并把 stdout/stderr 追加到日志：SDK 的 WS 连接/报错
# 不再只进看不见的窗口（10-30 事故：WS 首连卡死，bot 活着但聋、日志零线索）
$old = "$base\bot_console.old.log"
if (Test-Path "$base\bot_console.log") { Move-Item -Force "$base\bot_console.log" $old }
$cmdLine = "`"$py`" `"$base\bot.py`" >> `"$base\bot_console.log`" 2>&1"
Start-Process cmd -ArgumentList "/c $cmdLine" -WindowStyle Hidden
Write-Host "完成。发条消息验证，或看 bot.log 尾部。"
