#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面会话权限远程确认 hook（由 ~/.claude/settings.json 的 PermissionRequest 调起）

桌面 Claude 卡在权限确认时把确认转发到飞书：人不在电脑前 → 经 bot 的本地权限
通道发卡片，手机点 ✅/❌ 直接放行/拒绝桌面进程；人在电脑前 → 立即让路，终端
照常弹确认框（零打扰）。任何失败都让路：无输出 = Claude Code 走默认（终端弹框），
绝不干扰桌面会话。

stdin 是 PermissionRequest JSON（tool_name/tool_input/cwd/session_id；中文 description
经 cp936 错解会产生孤代理字符，须 errors=replace 容错）。应答输出：
{"hookSpecificOutput":{"hookEventName":"PermissionRequest",
                       "decision":{"behavior":"allow"|"deny"[,"message":…]}}}
（schema 与 --permission-prompt-tool 的 MCP 应答同构，实验定论于 2.1.312。）
"""
import json
import os
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "notify.log"
MUTE_FILE = BASE / "mute.flag"


def _log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def _load_env() -> dict:
    """轻量解析 .env（与 notify.py 一致：hook 里少 import 更快）。"""
    env = {}
    try:
        for line in (BASE / ".env").read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _user_idle_seconds() -> float:
    """Windows：距上次键鼠活动的秒数（人在电脑前≈小数值）；拿不到返回 -1。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        pass
    return -1.0


def main() -> int:
    if os.environ.get("FEISHU_BOT_CHILD") == "1":
        return 0  # bot 分身：权限已走 perm_mcp 的飞书卡，别发双卡
    # 手动静音（/mute）＝桌面事件推送全部暂停，权限确认回退终端
    if MUTE_FILE.exists():
        return 0

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        hook = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(hook, dict) or not hook.get("tool_name"):
        return 0

    # 人在电脑前：立即让路，终端弹框照常（与 notify.py 同一套免打扰阈值）
    env = _load_env()
    try:
        idle_threshold = float(env.get("NOTIFY_IDLE_SECONDS", "180") or 180)
    except ValueError:
        idle_threshold = 180.0
    idle = _user_idle_seconds()
    if 0 <= idle < idle_threshold:
        return 0

    port = int(env.get("FEISHU_PERM_PORT", "37921") or 37921)
    try:
        wait = float(env.get("PERM_GATE_WAIT", "300") or 300)
    except ValueError:
        wait = 300.0

    payload = {"type": "desktop_perm", "sid": hook.get("session_id") or "",
               "tool_name": hook.get("tool_name"),
               "tool_input": hook.get("tool_input"),
               "cwd": hook.get("cwd") or ""}
    decision = None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.settimeout(wait + 30)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = s.makefile("rb").readline()
        if line.strip():
            decision = json.loads(line)
    except Exception as e:
        _log(f"perm_gate 转发失败（回退终端确认）: {e!r}")
        return 0
    if not isinstance(decision, dict) or decision.get("behavior") not in ("allow", "deny"):
        return 0   # 超时无应答/无效应答：让路终端确认框
    out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                  "decision": {"behavior": decision["behavior"]}}}
    if decision.get("message"):
        out["hookSpecificOutput"]["decision"]["message"] = str(decision["message"])[:500]
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
