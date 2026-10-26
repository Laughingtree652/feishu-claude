#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面 Claude hook 通知脚本（由 ~/.claude/settings.json 的 hooks 调起）

事件：
  Stop         — 桌面 Claude 完成一个回合 → 「🏁 完成了一个回合」+ 最后回复摘要
  Notification — 桌面 Claude 需要确认/等待输入 → 「🔔 需要你」+ 提示内容

stdin 收到 hook JSON：{"session_id","transcript_path","cwd","hook_event_name","message"?}
机器人自己 spawn 的 claude 带 FEISHU_BOT_CHILD=1，直接跳过（bot 本身就会转发结果）。

设计原则：任何失败都静默退出 0，绝不干扰桌面会话。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
TARGET_FILE = BASE / "notify_target.json"
MUTE_FILE = BASE / "mute.flag"
LOG_FILE = BASE / "notify.log"


def _log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def _load_env() -> dict:
    """轻量解析 .env（避免依赖 python-dotenv，hook 里少 import 更快）。"""
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


def _last_text(transcript_path) -> str:
    """读转录文件尾部，提取最后一条 assistant 文本（给通知加摘要）。"""
    if not transcript_path:
        return ""
    try:
        p = Path(transcript_path)
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))  # 只看最后 64KB
            data = f.read().decode("utf-8", "replace")
        for line in reversed(data.splitlines()):
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            for b in (d.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    return b["text"].strip()
    except Exception:
        pass
    return ""


def _user_idle_seconds() -> float:
    """Windows：距上次键鼠活动的秒数（人在电脑前≈小数值）；拿不到返回 -1。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        pass
    return -1.0


def _send(app_id: str, app_secret: str, chat_id: str, text: str) -> bool:
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        api = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.ERROR) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            ).build()
        resp = api.im.v1.message.create(req)
        return bool(resp.success())
    except Exception as e:
        _log(f"send failed: {e!r}")
        return False


def main() -> int:
    import os
    if os.environ.get("FEISHU_BOT_CHILD") == "1":
        return 0  # 机器人自己的 claude 子进程，bot 已转发结果，跳过

    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    # 通知目标（bot 收到过消息才会存在）
    try:
        chat_id = json.loads(TARGET_FILE.read_text("utf-8"))["chat_id"]
    except Exception:
        return 0
    env = _load_env()
    app_id, app_secret = env.get("FEISHU_APP_ID"), env.get("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        return 0

    sid = hook.get("session_id") or ""
    cwd = hook.get("cwd") or ""
    event = hook.get("hook_event_name") or ""

    # 手动静音（飞书里 /mute）
    if MUTE_FILE.exists():
        return 0
    # 人在电脑前（最近有键鼠活动）就不打扰；离开超过阈值才推送
    try:
        idle_threshold = float(env.get("NOTIFY_IDLE_SECONDS", "180") or 180)
    except ValueError:
        idle_threshold = 180.0
    idle = _user_idle_seconds()
    if 0 <= idle < idle_threshold:
        return 0

    proj = Path(cwd).name if cwd else ""
    if event == "Notification":
        text = "🔔 桌面 Claude 需要你：" + (hook.get("message") or "")
    else:  # Stop（完成一个回合）
        summary = _last_text(hook.get("transcript_path"))
        text = "🏁 桌面 Claude 完成了一个回合"
        if summary:
            text += "\n" + summary[:300]
    if sid:
        where = f"[{proj}] " if proj else ""
        text += f"\n\n{where}会话 {sid[:8]}…\n/takeover {sid}  ← 在飞书接管该会话"

    _send(app_id, app_secret, chat_id, text)
    return 0  # 永远成功，不打断桌面会话


if __name__ == "__main__":
    sys.exit(main())
