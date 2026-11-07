# 一键重启飞书机器人：杀掉现有实例，再启动一个新的独立窗口
# 用法（PowerShell 里）：  powershell -ExecutionPolicy Bypass -File D:\work\feishu-claude\restart.ps1
#                      （保活拉起时传 -Reason keepalive，启动通知据此区分文案）
param([string]$Reason = "manual")
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

Write-Host "启动机器人…（隐藏窗口，输出落 logs\bot_console.log / logs\bot_err.log）"
# 隐藏窗口 + 输出重定向：SDK 的 WS 连接/报错不再只进看不见的窗口
# （10-30 事故：WS 首连卡死，bot 活着但聋、日志零线索）
$logs = "$base\logs"
if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs | Out-Null }
# 上一轮的根目录残留日志（老布局）一并收编进 logs/
foreach ($n in @("bot_console.log", "bot_err.log")) {
    if (Test-Path "$base\$n") { Move-Item -Force "$base\$n" "$logs\old_$n" }
}
if (Test-Path "$logs\bot_console.log") { Move-Item -Force "$logs\bot_console.log" "$logs\bot_console.old.log" }
if (Test-Path "$logs\bot_err.log") { Move-Item -Force "$logs\bot_err.log" "$logs\bot_err.old.log" }
# 重定向兜底：常规日志文件若被占用/重定向失败（10-31 深夜保活链下发生过，
# 实例 stdout 全程黑洞，排障零线索），换时间戳文件名，确保输出一定有落点。
$stamp = Get-Date -Format "MMdd_HHmmss"
try {
    Start-Process -FilePath $py -ArgumentList "$base\bot.py" -WorkingDirectory $base `
        -WindowStyle Hidden -ErrorAction Stop `
        -RedirectStandardOutput "$logs\bot_console.log" -RedirectStandardError "$logs\bot_err.log"
} catch {
    Write-Host "常规日志文件不可用（$($_.Exception.Message)），改用时间戳文件"
    Start-Process -FilePath $py -ArgumentList "$base\bot.py" -WorkingDirectory $base `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$logs\bot_console_$stamp.log" -RedirectStandardError "$logs\bot_err_$stamp.log"
}
# 启动来源标记：bot 起来后按值区分通知文案（bot.py 读后自删）——manual=手动
# 重启报"部署重启完成"；keepalive=保活链拉起报"自愈恢复"。无标记的启动（开机
# 自启等）也发，但按 10 分钟节流防崩溃循环刷屏
Set-Content -Path "$base\data\boot_reason.flag" -Value $Reason -Encoding ASCII

Write-Host "完成。发条消息验证，或看 logs\bot_console.log。"
