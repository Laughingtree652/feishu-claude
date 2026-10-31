' 保活计划任务的无窗口入口：计划任务跑 wscript.exe 执行本文件，
' 用 WScript.Shell 以隐藏窗口（第二参数 0）拉起 keepalive.ps1，
' 避免每 5 分钟闪一次 PowerShell 黑框。
' 卸载整个保活：schtasks /delete /tn FeishuClaudeKeepAlive /f
CreateObject("WScript.Shell").Run _
    "powershell -NoProfile -ExecutionPolicy Bypass -File D:\work\feishu-claude\keepalive.ps1", 0, False
