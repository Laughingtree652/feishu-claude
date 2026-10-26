#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 ↔ 本地 Claude Code 桥接器（WebSocket 长连接，无需公网 IP）

飞书发消息给机器人 → 调用本地 claude CLI（多轮会话自动续接）→
Claude 的回复与工具调用实时转发回飞书，实现远程操控。

可用命令：/help /new /stop /status /session /sessions /takeover /cwd /mode /mute /unmute
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from dotenv import load_dotenv

# Windows 控制台/IDE 重定向输出时避免 GBK 编码崩溃
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------- 配置（均可在 .env 覆盖） ----------------
APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]

WORK_DIR = Path(os.environ.get("WORK_DIR", str(BASE_DIR))).resolve()  # Claude 的工作目录
ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))     # 单次任务超时（秒）
FORWARD_TOOLS = os.environ.get("FORWARD_TOOLS", "1").strip().lower() not in ("0", "false", "no")
CLAUDE_EXTRA_ARGS = os.environ.get("CLAUDE_EXTRA_ARGS", "").strip()
CLAUDE_PATH = os.environ.get("CLAUDE_PATH") or shutil.which("claude")

SESSION_FILE = BASE_DIR / "sessions.json"
MAX_CHUNK = 3500      # 单条飞书消息最大字符数，超长自动分段
SEND_INTERVAL = 0.5   # 发消息最小间隔（秒），避免触发飞书频控

HELP_TEXT = """📖 命令列表
/help — 查看本帮助
/new — 开启新会话（清除历史上下文）
/stop — 中断正在执行的任务
/status — 查看当前任务状态
/session — 查看当前会话 ID（在电脑上 claude --resume <id> 可接续）
/sessions [条数] — 列出电脑上最近的会话（含当前绑定的）
/takeover [序号|会话ID|latest] — 接管电脑上的会话，带上全部上下文
/rename [名字] — 给当前会话起名（/sessions 列表优先显示名字）
/cwd [路径] — 查看 / 切换 Claude 的工作目录
/mode — 查看当前权限模式
/mute · /unmute — 暂停/恢复桌面事件推送

直接发送文本 = 给 Claude 下指令，多轮对话自动保持上下文。"""

# 用于发消息的 API 客户端（与收消息的 WebSocket 客户端相互独立）
api = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .log_level(lark.LogLevel.WARNING) \
    .build()

# ---------------- 会话持久化（chat_id -> {session_id, work_dir, updated}） ----------------
_sessions_lock = threading.Lock()
_sessions: dict = {}
if SESSION_FILE.exists():
    try:
        _sessions = json.loads(SESSION_FILE.read_text("utf-8"))
    except Exception:
        _sessions = {}

# ---------------- 会话命名（session_id -> 名字，/rename 维护） ----------------
NAMES_FILE = BASE_DIR / "names.json"
_names_lock = threading.Lock()
_names: dict = {}
if NAMES_FILE.exists():
    try:
        _names = json.loads(NAMES_FILE.read_text("utf-8"))
    except Exception:
        _names = {}


def save_names() -> None:
    with _names_lock:
        try:
            NAMES_FILE.write_text(json.dumps(_names, ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            pass


def save_sessions() -> None:
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, ensure_ascii=False, indent=1), "utf-8")


def _lookup_session(chat_id: str):
    """会话绑定工作目录：目录变了就视为无会话。"""
    sess = _sessions.get(chat_id) or {}
    if sess.get("work_dir") == str(WORK_DIR):
        return sess.get("session_id")
    return None


# ---------------- 运行中的任务（chat_id -> {"proc","started","reason"}） ----------------
_running_lock = threading.Lock()
_running: dict = {}


def kill_task(proc: subprocess.Popen) -> None:
    """终止 claude 进程树（Windows 下 .cmd 垫片会派生 node 子进程，必须整树终止）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


# ---------------- 发送消息 ----------------
_send_lock = threading.Lock()
_last_send = 0.0


def send_text(chat_id: str, text: str) -> None:
    """发送文本到指定会话：超长自动分段，带简单限速。"""
    global _last_send
    text = (text or "").strip()
    if not text:
        return
    chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
    for chunk in chunks:
        with _send_lock:
            gap = _last_send + SEND_INTERVAL - time.time()
            if gap > 0:
                time.sleep(gap)
            _last_send = time.time()
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": chunk}, ensure_ascii=False))
                    .build()
                ).build()
            try:
                resp = api.im.v1.message.create(req)
                if not resp.success():
                    print(f"[发消息失败] code={resp.code} msg={resp.msg}")
            except Exception as e:
                print(f"[发消息异常] {e!r}")


# ---------------- Claude 执行 ----------------
def _tool_summary(name: str, tool_input) -> str:
    """把工具调用入参压缩成一行摘要。"""
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "cmd", "script", "file_path", "path", "pattern", "url", "query", "description"):
        v = tool_input.get(key)
        if v:
            s = str(v).replace("\n", " ").strip()
            return f"{name} ▸ {s[:120]}"
    return name


def run_task(chat_id: str, prompt: str) -> None:
    """任务入口：同一会话同时只跑一个，忙时提示。"""
    with _running_lock:
        if chat_id in _running:
            send_text(chat_id, "⏳ 上一条任务还在执行中，发送 /stop 可中断，或稍后再发。")
            return
        slot = {"proc": None, "started": time.time(), "reason": None}
        _running[chat_id] = slot
    try:
        _run_claude(chat_id, prompt, slot)
    except Exception as e:
        send_text(chat_id, f"❌ 桥接器内部错误：{e!r}")
    finally:
        with _running_lock:
            _running.pop(chat_id, None)


def _run_claude(chat_id: str, prompt: str, slot: dict, ack: bool = True) -> None:
    """调用 claude -p（stream-json），实时转发输出。"""
    session_id = _lookup_session(chat_id)
    if ack:
        tag = f"续接会话 {session_id[:8]}…" if session_id else "新会话"
        send_text(chat_id, f"🤖 收到（{tag}），开始执行…")

    # 注意：提示词通过 stdin 传入，避免经 .cmd 垫片时被 cmd.exe 解析特殊字符
    cmd = [CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose"]
    if PERMISSION_MODE:
        cmd += ["--permission-mode", PERMISSION_MODE]
    if CLAUDE_EXTRA_ARGS:
        cmd += shlex.split(CLAUDE_EXTRA_ARGS)
    if session_id:
        cmd += ["--resume", session_id]

    # FEISHU_BOT_CHILD=1：让 notify.py 跳过机器人自己的 claude 子进程，避免重复通知
    child_env = {**os.environ, "FEISHU_BOT_CHILD": "1"}
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(WORK_DIR), env=child_env,
        )
    except FileNotFoundError:
        send_text(chat_id, "❌ 找不到 claude 命令，请在 .env 中用 CLAUDE_PATH 指定完整路径。")
        return
    slot["proc"] = proc
    try:
        proc.stdin.write(prompt)
    except (BrokenPipeError, OSError):
        pass  # 进程已提前退出（如参数错误），继续读输出让错误信息浮出来
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    # 超时看门狗
    def _timeout() -> None:
        slot["reason"] = "timeout"
        kill_task(proc)

    timer = threading.Timer(CLAUDE_TIMEOUT, _timeout)
    timer.daemon = True
    timer.start()

    # stderr 用单独线程收集，防止管道写满造成死锁
    stderr_buf: list = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_buf.append(line)

    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_err.start()

    last_text = ""   # 已转发过的最后一段 assistant 文本（用于和最终 result 去重）
    result = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue  # 忽略夹杂的非 JSON 行
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "assistant":
                for block in (evt.get("message") or {}).get("content") or []:
                    btype = block.get("type")
                    if btype == "text" and (block.get("text") or "").strip():
                        last_text = block["text"].strip()
                        send_text(chat_id, last_text)
                    elif btype == "tool_use" and FORWARD_TOOLS:
                        send_text(chat_id, "🔧 " + _tool_summary(block.get("name", "?"), block.get("input")))
            elif etype == "result":
                result = evt
    finally:
        timer.cancel()
    returncode = proc.wait()
    t_err.join(timeout=2)
    tail = "".join(stderr_buf[-12:]).strip()

    # --resume 失效（目录切换/记录损坏等）→ 清掉会话自动重试一次
    if slot.get("reason") is None and returncode != 0 and result is None \
            and session_id and ("session" in tail.lower() or "conversation" in tail.lower()):
        with _sessions_lock:
            _sessions.pop(chat_id, None)
        save_sessions()
        send_text(chat_id, "🔄 原会话已失效，自动开启新会话重试…")
        _run_claude(chat_id, prompt, slot, ack=False)
        return

    _finish(chat_id, result, returncode, tail, last_text, slot)


def _finish(chat_id: str, result, returncode: int, tail: str, last_text: str, slot: dict) -> None:
    """收尾：保存会话、汇报结果。"""
    reason = slot.get("reason")
    if reason == "timeout":
        send_text(chat_id, f"⚠️ 执行超过 {CLAUDE_TIMEOUT}s，已强制中断。可在 .env 中调大 CLAUDE_TIMEOUT。")
        return
    if reason == "stop":
        send_text(chat_id, "⛔ 已手动中断。")
        return
    if result is None:
        send_text(chat_id, f"❌ Claude 异常退出（code={returncode}）\n{tail[-800:] or '（无错误输出）'}")
        return

    # 记录会话 ID，下一条消息自动 --resume 续接
    sid = result.get("session_id")
    if sid:
        with _sessions_lock:
            _sessions[chat_id] = {
                "session_id": sid,
                "work_dir": str(WORK_DIR),
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
        save_sessions()

    final_text = (result.get("result") or "").strip()
    if final_text and final_text != last_text:
        send_text(chat_id, final_text)

    dur = (result.get("duration_ms") or 0) / 1000
    cost = result.get("total_cost_usd") or 0
    turns = result.get("num_turns", "?")
    line = f"✅ 完成 · 耗时 {dur:.1f}s · {turns} 轮对话"
    if cost:
        line += f" · ${cost:.3f}"
    if result.get("is_error"):
        line = "⚠️ 任务异常结束 · " + line
    send_text(chat_id, line)

    denials = result.get("permission_denials") or []
    if denials:
        send_text(chat_id, f"⚠️ 有 {len(denials)} 个操作因权限被拒绝。"
                           "如需放开：.env 中设置 CLAUDE_PERMISSION_MODE=bypassPermissions（注意风险）。")


# ---------------- 会话接管（电脑 → 飞书） ----------------
def _session_cwd(session_file: Path):
    """从会话转录文件开头提取该会话的工作目录（cwd 字段）。"""
    try:
        with session_file.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("cwd"):
                    return d["cwd"]
    except OSError:
        pass
    return None


def _session_preview(session_file: Path) -> str:
    """会话最近一条真实用户输入（比首条消息更贴近当前话题）。

    注意转录里 tool_result 记录的 type 也是 "user"，需按内容形态排除；
    isMeta 的是系统注入消息，同样跳过。
    """
    try:
        with session_file.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))  # 只看最后 64KB
            data = fh.read().decode("utf-8", "replace")
        for line in reversed(data.splitlines()):
            if '"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "user" or d.get("isMeta"):
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, str) and c.strip():
                return c.strip().replace("\n", " ")[:40]
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                        return b["text"].strip().replace("\n", " ")[:40]
    except OSError:
        pass
    return ""


def _rel_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return f"{int(d)}秒前"
    if d < 3600:
        return f"{int(d / 60)}分钟前"
    if d < 86400:
        return f"{int(d / 3600)}小时前"
    return f"{int(d / 86400)}天前"


def _recent_sessions(limit: int = 8, include_managed: bool = False):
    """最近的会话，按活跃时间倒序。

    include_managed=False（默认）：排除机器人管理的会话（/takeover latest 用，
    语义是"找桌面上的会话"）；include_managed=True：全部列出并标注归属
    （/sessions 列表用——用户需要看到包括当前绑定在内的全部会话）。
    """
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    with _sessions_lock:
        managed = {v.get("session_id"): chat for chat, v in _sessions.items()
                   if isinstance(v, dict) and v.get("session_id")}
    items = []
    for f in root.glob("*/*.jsonl"):
        if f.stem in managed and not include_managed:
            continue
        try:
            items.append((f.stat().st_mtime, f))
        except OSError:
            continue
    items.sort(reverse=True)
    return [{"session_id": f.stem, "cwd": _session_cwd(f),
             "mtime": m, "preview": _session_preview(f),
             "managed_chat": managed.get(f.stem)}
            for m, f in items[:limit]]


def _find_session_file(sid: str):
    """在 ~/.claude/projects/*/ 下查找 <sid>.jsonl，返回 (文件, cwd) 或 (None, None)。"""
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return None, None
    for f in root.glob(f"*/{sid}.jsonl"):
        return f, _session_cwd(f)
    return None, None


def _latest_desktop_session():
    """最近活跃、且不归机器人管的会话（即最近的『桌面』会话）。"""
    items = _recent_sessions(1)
    if not items:
        return None, None
    return items[0]["session_id"], items[0]["cwd"]


# ---------------- 命令处理 ----------------
def handle_command(chat_id: str, text: str) -> None:
    global WORK_DIR
    parts = text.split()
    cmd = parts[0].lower()
    arg = text[len(parts[0]):].strip()

    if cmd == "/help":
        send_text(chat_id, HELP_TEXT)
    elif cmd == "/new":
        with _sessions_lock:
            _sessions.pop(chat_id, None)
        save_sessions()
        send_text(chat_id, "🆕 已开启新会话，下一条消息不带历史上下文。")
    elif cmd == "/stop":
        with _running_lock:
            slot = _running.get(chat_id)
        if slot and slot.get("proc"):
            slot["reason"] = "stop"
            kill_task(slot["proc"])
            send_text(chat_id, "🛑 正在中断当前任务…")
        else:
            send_text(chat_id, "当前没有正在执行的任务。")
    elif cmd == "/status":
        with _running_lock:
            slot = _running.get(chat_id)
        if slot:
            send_text(chat_id, f"⏳ 任务执行中，已运行 {int(time.time() - slot['started'])}s（/stop 可中断）")
        else:
            send_text(chat_id, "空闲中。")
    elif cmd == "/session":
        sess = _sessions.get(chat_id) or {}
        sid = sess.get("session_id")
        if sid:
            name = _names.get(sid)
            show = f"{sid}" + (f"「{name}」" if name else "") + \
                   f"\n（在本机执行 claude --resume {sid} 可接续此会话）"
        else:
            show = "无"
        send_text(chat_id, f"当前会话：{show}\n工作目录：{WORK_DIR}")
    elif cmd == "/rename":
        sess = _sessions.get(chat_id) or {}
        sid = sess.get("session_id")
        if not arg:
            if not sid:
                send_text(chat_id, "当前聊天还没有会话，先发条消息或 /takeover 后再命名。")
            else:
                cur = _names.get(sid) or "（未命名）"
                send_text(chat_id, f"当前会话名：{cur}\n用法：/rename 新名字")
            return
        if not sid:
            send_text(chat_id, "当前聊天还没有会话，先发条消息或 /takeover 后再命名。")
            return
        name = arg[:30]
        with _names_lock:
            _names[sid] = name
        save_names()
        send_text(chat_id, f"✅ 当前会话已命名：「{name}」（/sessions 列表会优先显示）")
    elif cmd == "/takeover":
        want = arg or "latest"
        force = want.endswith("!")
        if force:
            want = want[:-1].strip() or "latest"
        sid = cwd = None
        if want.lower() == "latest":
            sid, cwd = _latest_desktop_session()
        elif want.isdigit():
            # 与 /sessions 同一份列表（含机器人管理的会话），序号才对得上
            items = _recent_sessions(8, include_managed=True)
            idx = int(want) - 1
            if 0 <= idx < len(items):
                sid, cwd = items[idx]["session_id"], items[idx]["cwd"]
        else:
            sid, cwd = _find_session_file(want)
        if not sid:
            send_text(chat_id, f"❌ 找不到会话 {want}\n"
                               "提示：电脑上执行 claude --resume 可查看会话列表和 ID；"
                               "桌面通知里也带有可复制的 /takeover 命令。")
            return
        cur = (_sessions.get(chat_id) or {}).get("session_id")
        if cur == sid:
            send_text(chat_id, f"✅ 这就是本聊天当前的会话（{sid[:8]}…），直接发消息即可。")
            return
        # 活跃检测：转录文件最近仍在被写入 = 会话很可能还在电脑上运行，
        # 同时接管会导致两边上下文分叉（实测：不报错但历史交错），需强制确认
        session_file, _ = _find_session_file(sid)
        if session_file and not force:
            try:
                age = time.time() - session_file.stat().st_mtime
            except OSError:
                age = None
            if age is not None and age < 15:
                send_text(chat_id,
                          f"⚠️ 会话 {sid[:8]}… {int(age)} 秒前仍在活动，很可能还在电脑上运行。\n"
                          "两边同时使用会导致上下文分叉（回复互相看不见）。\n"
                          f"等它跑完（🏁 通知）或退出桌面会话后重试；\n"
                          f"确认要现在接管，发 /takeover {want}! 强制接管。")
                return
        # 会话属于哪个目录，就把工作目录切过去（resume 按目录查找会话）
        if cwd and Path(cwd).is_dir():
            WORK_DIR = Path(cwd).resolve()
        with _sessions_lock:
            _sessions[chat_id] = {
                "session_id": sid,
                "work_dir": str(WORK_DIR),
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
        save_sessions()
        send_text(chat_id,
                  f"✅ 已接管会话 {sid[:8]}…（目录：{WORK_DIR}）\n"
                  "下一条消息将带上该会话的全部上下文。\n"
                  "⚠️ 若桌面那边还开着，请先退出（避免两边同时写入）。")
    elif cmd == "/sessions":
        try:
            n = max(2, min(20, int(arg))) if arg else 8
        except ValueError:
            n = 8
        items = _recent_sessions(n, include_managed=True)
        if not items:
            send_text(chat_id, "没找到会话。")
            return
        lines = [f"📋 最近的会话（/takeover 序号 即可接管）："]
        for i, it in enumerate(items, 1):
            proj = Path(it["cwd"]).name if it["cwd"] else "?"
            active = " ⚡可能仍在运行" if time.time() - it["mtime"] < 15 else ""
            owner = it.get("managed_chat")
            mine = "（本聊天当前会话）" if owner == chat_id else (
                "（另一个飞书聊天在用）" if owner else "")
            name = _names.get(it["session_id"])
            lines.append(f"{i}. [{proj}] {_rel_time(it['mtime'])} · {it['session_id'][:8]}…{active}{mine}")
            if name:
                lines.append(f"   📛 {name}")
            elif it["preview"]:
                lines.append(f"   「{it['preview']}」")
        send_text(chat_id, "\n".join(lines))
    elif cmd == "/mute":
        try:
            (BASE_DIR / "mute.flag").write_text(
                datetime.now().isoformat(timespec="seconds"), "utf-8")
        except OSError:
            pass
        send_text(chat_id, "🔕 桌面事件推送已暂停（/unmute 恢复）。\n"
                           "另外：检测到你最近 3 分钟有键鼠活动时也会自动不打扰。")
    elif cmd == "/unmute":
        try:
            (BASE_DIR / "mute.flag").unlink()
        except OSError:
            pass
        send_text(chat_id, "🔔 桌面事件推送已恢复。")
    elif cmd == "/cwd":
        if not arg:
            send_text(chat_id, f"当前工作目录:{WORK_DIR}\n用法:/cwd D:\\path\\to\\project")
        else:
            p = Path(arg)
            if not p.is_dir():
                send_text(chat_id, f"❌ 目录不存在：{p}")
            else:
                WORK_DIR = p.resolve()
                with _sessions_lock:
                    _sessions.clear()  # 会话绑定目录，切换后全部重置
                save_sessions()
                send_text(chat_id, f"📁 工作目录已切换：{WORK_DIR}\n（历史会话已重置）")
    elif cmd == "/mode":
        send_text(chat_id, f"当前权限模式：{PERMISSION_MODE or '未设置（default）'}\n"
                           "可选值见 .env 中 CLAUDE_PERMISSION_MODE 的注释。")
    else:
        send_text(chat_id, f"未知命令 {cmd}，发送 /help 查看可用命令。")


# ---------------- 消息入口 ----------------
def on_message(event: P2ImMessageReceiveV1) -> None:
    """WebSocket 事件回调：只做快速解析与分发，重活交给工作线程（避免阻塞事件循环）。"""
    try:
        data = event.event
        msg = data.message
        if msg is None:
            return
        chat_id = msg.chat_id
        if msg.message_type != "text":
            send_text(chat_id, "⚠️ 目前只支持文本消息。")
            return

        sender_id = ""
        if data.sender and data.sender.sender_id:
            sender_id = data.sender.sender_id.open_id or ""

        # 发送者白名单：机器人等于电脑的远程入口，必须限制
        if not ALLOWED_USERS:
            # 记录首个发消息的人，便于自动完成白名单配置
            try:
                (BASE_DIR / "pending_user.json").write_text(
                    json.dumps({"chat_id": chat_id, "open_id": sender_id,
                                "updated": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False), "utf-8")
            except OSError:
                pass
            send_text(chat_id,
                      "🔒 尚未配置发送者白名单，出于安全考虑暂不执行任何指令。\n\n"
                      f"你的 open_id：{sender_id}\n\n"
                      "请编辑 .env 设置 ALLOWED_USERS=<上面的 open_id>，然后重启机器人。")
            return
        if sender_id not in ALLOWED_USERS:
            send_text(chat_id, "🔒 你不在白名单（ALLOWED_USERS）内，无法使用该机器人。")
            return

        # 记录通知目标：桌面 hook 的 notify.py 读它把提醒推到这个聊天（最后活跃的聊天优先）
        try:
            (BASE_DIR / "notify_target.json").write_text(
                json.dumps({"chat_id": chat_id, "open_id": sender_id,
                            "updated": datetime.now().isoformat(timespec="seconds")},
                           ensure_ascii=False), "utf-8")
        except OSError:
            pass

        try:
            text = json.loads(msg.content or "{}").get("text", "")
        except json.JSONDecodeError:
            return

        # 群聊：必须 @机器人（任意 @ 即触发，白名单负责拦截外人），并去掉 @ 占位符
        if msg.chat_type == "group":
            if not msg.mentions:
                return
            for m in msg.mentions:
                text = text.replace(m.key or "", "")
            text = text.strip()
            if not text:
                return

        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            handle_command(chat_id, text)
            return

        threading.Thread(target=run_task, args=(chat_id, text), daemon=True).start()
    except Exception as e:
        print(f"[处理消息出错] {e!r}")


def main() -> None:
    if not CLAUDE_PATH:
        sys.exit("❌ 找不到 claude 命令。请确认已安装 Claude Code 并在 PATH 中，"
                 "或在 .env 中设置 CLAUDE_PATH=claude 的完整路径")
    if not WORK_DIR.is_dir():
        sys.exit(f"❌ 工作目录不存在：{WORK_DIR}")
    valid_modes = {"", "default", "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
    if PERMISSION_MODE not in valid_modes:
        sys.exit(f"❌ CLAUDE_PERMISSION_MODE 无效：{PERMISSION_MODE}\n"
                 f"   可选值：{', '.join(sorted(m for m in valid_modes if m))}")

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    ws = lark.ws.Client(APP_ID, APP_SECRET,
                        event_handler=handler,
                        log_level=lark.LogLevel.INFO,
                        auto_reconnect=True)

    print("✅ 飞书机器人已启动（WebSocket 长连接，无需公网 IP）")
    print(f"   Claude：{CLAUDE_PATH}")
    print(f"   工作目录：{WORK_DIR}")
    print(f"   权限模式：{PERMISSION_MODE or 'default'}｜超时：{CLAUDE_TIMEOUT}s｜转发工具调用：{'开' if FORWARD_TOOLS else '关'}")
    print(f"   白名单：{len(ALLOWED_USERS)} 人" + ("" if ALLOWED_USERS else "（未配置，首条消息会提示如何配置）"))
    print("   等待消息中…（Ctrl+C 退出）")
    ws.start()  # 阻塞，断线自动重连


if __name__ == "__main__":
    main()
