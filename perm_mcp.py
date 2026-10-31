#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code 权限确认 MCP 服务器（被 claude 作为 stdio MCP 子进程拉起）。

配合 bot.py 的 --permission-prompt-tool 使用：claude 无头模式下遇到权限确认
时调用本工具 → 通过本地 TCP 把请求转给 bot 进程 → bot 发飞书卡片等你点
按钮/回文字 → 决定原路返回给 claude。

环境变量（由 bot.py 在拉起 claude 时注入，claude 会传给自己的 MCP 子进程）：
  FEISHU_CHAT_ID   发起任务的飞书聊天（卡片路由用）
  FEISHU_PERM_PORT bot 监听的本地端口（默认 37921）

返回给 claude 的决定（JSON 文本）：
  {"behavior": "allow"}                            允许本次执行
  {"behavior": "deny", "message": "..."}           拒绝，message 会转给 Claude
"""
import json
import os
import socket
import sys

TOOL_NAME = "request_permission"
DEFAULT_PORT = "37921"


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ask_bot(args: dict) -> dict:
    """把权限请求转发给 bot 进程，阻塞等用户的决定。"""
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    port = int(os.environ.get("FEISHU_PERM_PORT") or DEFAULT_PORT)
    payload = {"type": "perm", "chat_id": chat_id,
               "tool_name": args.get("tool_name") or args.get("toolName") or "?",
               "tool_input": args.get("tool_input") or args.get("input") or {},
               "suggestions": args.get("suggestions") or []}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=15) as s:
            # errors=replace：部分宿主传来的中文会带残缺代理字符，严格编码会直接抛异常
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8", errors="replace"))
            # 无限等待用户的决定（与桌面终端一致）；bot 挂掉时连接断开，走"通道不可用"兜底
            s.settimeout(None)
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        if not data.strip():
            return {"behavior": "deny", "message": "权限确认无响应"}
        result = json.loads(data.decode("utf-8"))
        return result if isinstance(result, dict) and result.get("behavior") in ("allow", "deny") \
            else {"behavior": "deny", "message": f"权限确认返回异常：{result!r}"}
    except Exception as e:
        return {"behavior": "deny", "message": f"权限确认通道不可用：{e!r}"}


def main() -> None:
    """极简 stdio MCP：newline 分隔的 JSON-RPC，只实现 initialize/tools/list/tools/call。"""
    # MCP stdio 协议是 UTF-8，而 Windows 管道下 Python 默认按 cp936 解码——
    # 中文 tool_input 会错解成乱码（飞书卡片上就是乱码），显式改为 UTF-8。
    # stdout 同理：拒绝原因里的中文要原路传回 claude。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "feishu-perm", "version": "1.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": TOOL_NAME,
                "description": "向飞书用户请求工具执行权限确认（点卡片按钮或回复文字）",
                "inputSchema": {"type": "object", "properties": {
                    "tool_name": {"type": "string", "description": "待确认的工具名"},
                    "tool_input": {"type": "object", "description": "工具入参"},
                    "suggestions": {"type": "array", "description": "建议的选项"}},
                    "required": ["tool_name", "tool_input"]}}]}})
        elif method == "tools/call":
            args = (req.get("params") or {}).get("arguments") or {}
            decision = ask_bot(args)
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}]}})
        elif method.startswith("notifications/"):
            pass  # 通知类消息无需应答
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
