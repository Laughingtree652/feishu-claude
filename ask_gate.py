#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面会话选择题远程应答 hook（由 ~/.claude/settings.json 的 PreToolUse 调起，
matcher 仅 AskUserQuestion——权限确认走 perm_gate.py，两个 hook 各管一段）。

Claude 在终端里调 AskUserQuestion（原生选择 UI）时把问题转发到飞书：
被接管的会话 / 人不在电脑前 → 经 bot 的本地通道推选择卡，用户点选项或
回文字，答复组装成 block reason 以「退出码 2 + stderr」原路反馈给 Claude
（它按答复继续工作，不再重复提问，终端原生 UI 不弹）；
未接管 + 人在电脑前 → 立即让路，终端弹原生选择 UI（免打扰）。
任何失败/超时/静音都让路（无输出退出 0），原生 UI 接手，绝不卡死。

stdin 是 PreToolUse JSON（tool_name/tool_input/session_id/cwd；中文经
cp936 错解会产生孤代理字符，须 errors=replace 容错）。
"""
import json
import os
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "logs" / "notify.log"   # 日志集中进 logs/（与 bot.py 同布局）
LOG_FILE.parent.mkdir(exist_ok=True)
MUTE_FILE = BASE / "data" / "mute.flag"   # 状态进 data/；老布局位置兜底着查


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
        return 0  # bot 分身：无头模式不弹 UI；其提问本就应由 bot 转发（勿双卡）
    # 手动静音（/mute）＝桌面事件推送全部暂停，选择回退终端原生 UI
    if MUTE_FILE.exists() or (BASE / "mute.flag").exists():
        return 0

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        hook = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(hook, dict) or hook.get("tool_name") != "AskUserQuestion":
        return 0   # matcher 只配 AskUserQuestion，这里再保险一道
    qs = (hook.get("tool_input") or {}).get("questions") or []
    if not isinstance(qs, list) or not qs:
        return 0

    env = _load_env()

    # 接管态判定（perm_gate.py 同款）：sid 被某飞书聊天接管过 → 响应面在
    # 飞书，无视"人在电脑前"让路——用户正盯飞书（打字让键鼠活跃），活跃
    # 证明不了看得见终端选择框，让路＝干等（11-02 与 notify/perm_gate 同坑）
    route = {}
    for p in (BASE / "data" / "desktop_route.json", BASE / "desktop_route.json"):
        try:
            route = json.loads(p.read_text(encoding="utf-8"))
            break
        except Exception:
            continue
    sid = str(hook.get("session_id") or "")
    taken_over = bool(str(route.get(sid) or ""))
    # 未接管：查 idle——人在电脑前让路（响应面在终端，免打扰）；
    # 人不在电脑前 → away=True（回退终端弹给谁看？没人看的框是死通道，
    # 飞书卡须不限时等答复）；idle=-1 检测失败保守按可超时（away=False）
    away = False
    if not taken_over:
        try:
            idle_threshold = float(env.get("NOTIFY_IDLE_SECONDS", "180") or 180)
        except ValueError:
            idle_threshold = 180.0
        idle = _user_idle_seconds()
        if 0 <= idle < idle_threshold:
            _log(f"ask_gate: questions={len(qs)} sid={sid[:8]} taken_over=False "
                 f"idle={idle:.0f}s → 让路终端")
            return 0
        away = idle >= idle_threshold
    _log(f"ask_gate: questions={len(qs)} sid={sid[:8]} "
         f"taken_over={taken_over} away={away}")

    port = int(env.get("FEISHU_PERM_PORT", "37921") or 37921)
    try:
        wait = float(env.get("PERM_GATE_WAIT", "300") or 300)
    except ValueError:
        wait = 300.0

    payload = {"type": "desktop_ask", "sid": sid, "questions": qs, "away": away}
    reply = None
    try:
        # away（人不在电脑前）：bot 卡不限时，本连接同步不限时；
        # 否则按问题数放宽（bot 侧逐问推卡，最坏 N×wait）
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.settimeout(None if away else wait * len(qs) + 60)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = s.makefile("rb").readline()
        if line.strip():
            reply = json.loads(line)
    except Exception as e:
        _log(f"ask_gate 转发失败（回退终端选择 UI）: {e!r}")
        return 0
    if not isinstance(reply, dict) or not reply.get("reason"):
        return 0   # 超时无应答/无路由/无效应答：让路，终端原生选择 UI 接手
    # block：stderr 文本会作为工具反馈可见给 Claude，它按答复继续
    sys.stderr.write(str(reply["reason"]))
    return 2


if __name__ == "__main__":
    sys.exit(main())
