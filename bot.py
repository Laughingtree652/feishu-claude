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
import socket
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
    PatchMessageRequest,
    PatchMessageRequestBody,
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
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "0"))   # 单次任务超时（秒），<=0 不限时
FORWARD_TOOLS = os.environ.get("FORWARD_TOOLS", "1").strip().lower() not in ("0", "false", "no")
CLAUDE_EXTRA_ARGS = os.environ.get("CLAUDE_EXTRA_ARGS", "").strip()
CLAUDE_PATH = os.environ.get("CLAUDE_PATH") or shutil.which("claude")

# 权限模式全集（/mode 运行时切换 + 启动校验共用）
PERMISSION_MODES = {"default", "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
SHELL_TIMEOUT = 120  # 飞书 ! 命令直通 shell 的超时（秒）

# 权限确认卡片（--permission-prompt-tool → perm_mcp.py → 本地 socket → 飞书卡片）
PERM_PORT = int(os.environ.get("FEISHU_PERM_PORT", "37921"))     # 与 perm_mcp.py 约定的本地端口
PERM_TIMEOUT = int(os.environ.get("PERM_TIMEOUT_SECONDS", "600"))  # 等用户确认权限的上限（秒）
TAKEOVER_ASK_TIMEOUT = 60  # 接管运行中会话时等用户选卡片选项的上限（秒），超时默认排队

SESSION_FILE = BASE_DIR / "sessions.json"
MAX_CHUNK = 3500      # 单条飞书消息最大字符数，超长自动分段
SEND_INTERVAL = 0.5   # 发消息最小间隔（秒），避免触发飞书频控

HELP_TEXT = """📖 命令列表
/help — 查看本帮助
/new — 开启新会话（清除历史上下文）
/stop — 中断当前任务（排队的消息一并取消）
/status — 查看当前任务与排队状态
/session — 查看当前会话 ID（在电脑上 claude --resume <id> 可接续）
/sessions [条数] — 列出电脑上最近的会话（含当前绑定的）
/takeover [序号|会话ID|latest] — 接管电脑上的会话（在跑的发卡片选：排队/强制/取消）
/peek [序号|会话ID|latest] — 旁观会话当前进度（正在干什么、最新输出）
/unqueue — 清空排队（消息队列 / 排队接管）
/rename [名字] — 给当前会话起名（/sessions 列表优先显示名字）
/addcmd 标签 消息 · /delcmd 序号 · /cmds · /panel — 快捷命令按钮面板
/cwd [路径] — 查看 / 切换 Claude 的工作目录
/mode [模式] — 查看 / 切换权限模式（manual、auto、acceptEdits…）
/listen — 群聊免 @ 模式：本群所有消息直接发给机器人（适合只有你+机器人的群）
/mute · /unmute — 暂停/恢复桌面事件推送

直接发送文本 = 给 Claude 下指令，多轮对话自动保持上下文。
任务执行中发消息会自动排队，当前任务完成后依次执行。
Claude 需要权限确认时会发卡片，点按钮或直接回复文字（允许/拒绝/自定义说明）。
!命令 = 直接在本机 PowerShell 执行（不经 Claude），如 !git status"""

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

# ---------------- 快捷命令（卡片按钮面板，/addcmd 维护） ----------------
COMMANDS_FILE = BASE_DIR / "commands.json"
_commands_lock = threading.Lock()
_commands: list = []
if COMMANDS_FILE.exists():
    try:
        _commands = json.loads(COMMANDS_FILE.read_text("utf-8"))
        if not isinstance(_commands, list):
            _commands = []
    except Exception:
        _commands = []


def save_commands() -> None:
    with _commands_lock:
        try:
            COMMANDS_FILE.write_text(json.dumps(_commands, ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            pass


# ---------------- 免 @ 群（/listen 维护：只有自己+机器人的群里消息全量接收） ----------------
LISTEN_FILE = BASE_DIR / "listen.json"
_listen_lock = threading.Lock()
_listen: set = set()
if LISTEN_FILE.exists():
    try:
        _listen = set(json.loads(LISTEN_FILE.read_text("utf-8")))
    except Exception:
        _listen = set()
_chat_types: dict = {}   # chat_id -> "p2p"/"group"（on_message 记录，命令处理时用）
_hint_ts: dict = {}      # chat_id -> 上次"群里裸命令"提示时间（限频用）


def save_listen() -> None:
    with _listen_lock:
        try:
            LISTEN_FILE.write_text(json.dumps(sorted(_listen), ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            pass


def save_sessions() -> None:
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, ensure_ascii=False, indent=1), "utf-8")


def _lookup_session(chat_id: str):
    """返回聊天绑定的会话 ID。work_dir 固化在各聊天自己的绑定里（per-chat），
    不同聊天并行使用互不影响。"""
    sess = _sessions.get(chat_id) or {}
    return sess.get("session_id") or None


def _chat_work_dir(chat_id: str) -> Path:
    """聊天的工作目录：优先用会话绑定里固化的目录（随 /takeover 跟随会话），
    没有绑定则用全局默认 WORK_DIR。"""
    sess = _sessions.get(chat_id) or {}
    wd = sess.get("work_dir")
    if wd and Path(wd).is_dir():
        return Path(wd)
    return WORK_DIR


def _chat_label(chat_id: str) -> str:
    """聊天当前会话的标识「项目名·会话名或短ID」，回复时带上提醒用户当前在哪个会话。"""
    sess = _sessions.get(chat_id) or {}
    sid = sess.get("session_id")
    if not sid:
        return ""
    proj = Path(sess.get("work_dir") or str(WORK_DIR)).name
    tag = _names.get(sid) or (sid[:8] + "…")
    return f"[{proj}·{tag}]"


def _mode_label() -> str:
    """当前权限模式的简短标识（bypass 加警示）。"""
    m = PERMISSION_MODE or "default"
    return f"⚠️{m}" if m == "bypassPermissions" else m


# ---------------- 运行中的任务（键=会话ID或chat_id -> {"chat_id","proc","started","reason"}） ----------------
# 互斥/排队的粒度是「会话」：不同会话的任务互不阻塞（哪怕同一聊天先后 /takeover
# 切换）；同一会话同时只跑一个——两个聊天绑同一会话时后到的直接拒绝（防交错写）。
_running_lock = threading.Lock()
_running: dict = {}

# ---------------- 消息排队（任务忙时新消息入队，当前任务跑完自动依次执行；键同上） ----------------
_msg_queue_lock = threading.Lock()
_msg_queue: dict = {}   # 会话键 -> [str]


def _task_key(chat_id: str) -> str:
    """任务互斥键：聊天绑定了会话用会话 ID，否则用聊天自身（新会话彼此独立）。"""
    return (_sessions.get(chat_id) or {}).get("session_id") or chat_id

# ---------------- 排队接管（会话在电脑上跑着时先绑定，消息排队等回合结束自动执行） ----------------
_pending_lock = threading.Lock()
_pending: dict = {}        # chat_id -> {"sid","file","queue":[str],"started":float}
PENDING_TIMEOUT = 1800     # 排队等待上限（秒）


def _bind_session(chat_id: str, sid: str, cwd) -> None:
    """把会话绑定到聊天。工作目录跟随会话所在项目，但只固化进本聊天的绑定，
    不动全局 WORK_DIR——否则 A 聊天接管会破坏 B 聊天的会话续接。"""
    if cwd and Path(cwd).is_dir():
        wd = str(Path(cwd).resolve())
    else:
        wd = str(WORK_DIR)
    with _sessions_lock:
        _sessions[chat_id] = {
            "session_id": sid,
            "work_dir": wd,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
    save_sessions()


def _wait_and_drain(chat_id: str) -> None:
    """轮询排队中的会话，回合结束（空闲）后把排队消息依次执行。

    之所以可行：CLI 无法向运行中的进程注入消息，但机器人每条消息都是重新
    --resume 重读转录文件——等桌面回合把结果写完再发，上下文就是完整无缝的。
    """
    while True:
        time.sleep(5)
        with _pending_lock:
            pend = _pending.get(chat_id)
        if not pend:
            return  # 已被取消
        if time.time() - pend["started"] > PENDING_TIMEOUT:
            with _pending_lock:
                _pending.pop(chat_id, None)
            send_text(chat_id, "⏱ 排队等待超时（30 分钟），已取消排队。\n"
                               "会话绑定仍保留；/peek 确认它空闲后可直接发消息。")
            return
        state, _age = _session_state(pend["file"])
        if state in ("running", None):
            continue
        with _pending_lock:
            pend = _pending.pop(chat_id, None)
        if not pend:
            return
        if not pend["queue"]:
            send_text(chat_id, f"✅ 会话 {pend['sid'][:8]}… 已空闲，接管生效，随时发消息。")
            return
        send_text(chat_id, f"✅ 会话 {pend['sid'][:8]}… 已空闲，开始处理排队的 {len(pend['queue'])} 条消息…")
        for msg in pend["queue"]:
            run_task(chat_id, msg)   # run_task 自带互斥，顺序调用即串行
        return


def _takeover_running_dialog(chat_id: str, sid: str, cwd, session_file, age: float) -> None:
    """目标会话正在跑：发卡片让用户选 排队/强制/取消（60 秒无响应默认排队）。

    独立线程执行——handle_command 在 WS 事件循环里，不能阻塞等用户点按钮。
    """
    tag = _names.get(sid) or f"{sid[:8]}…"
    raw_text = None
    kind, val = _ask_choice(
        chat_id, "⏳ 会话正在运行",
        f"会话 **{tag}** 正在跑（{int(age)} 秒前仍在写入），怎么接管？",
        [{"label": "⏳ 排队等它结束", "value": "queue", "style": "primary"},
         {"label": "⚡ 强制立即接管", "value": "force", "style": "danger"},
         {"label": "❌ 取消", "value": "cancel", "style": "default"}],
        timeout=TAKEOVER_ASK_TIMEOUT, template="orange", tag="takeover",
        note="也可以直接回复文字：`排队` / `强制` / `取消`。60 秒未选默认排队。\n"
             "（老用法仍可用：/takeover <id>! 跳过选择直接强制接管）")
    if kind == "text":
        raw_text = val
        t = (val or "").strip().lower()
        val = "force" if t in ("强制", "force", "立即", "now") else \
              "cancel" if t in ("取消", "cancel", "算了", "不") else "queue"
    elif kind != "choice":
        val = "queue"   # 超时/被新提问顶替：默认排队（原行为）
    if val == "cancel":
        send_text(chat_id, "↩️ 已取消接管。")
        return
    if val == "force":
        _bind_session(chat_id, sid, cwd)
        send_text(chat_id,
                  f"✅ 已强制接管会话 {sid[:8]}…（目录：{_chat_work_dir(chat_id)}）\n"
                  "下一条消息将带上该会话的全部上下文。\n"
                  "⚠️ 桌面那边若还开着，两边同时用会分叉，建议先退出桌面端。")
        return
    # queue：绑定 + 排队接管（沿用 _wait_and_drain 轮询机制）
    _bind_session(chat_id, sid, cwd)
    keep = raw_text if (kind == "text" and raw_text and raw_text.strip()
                        and raw_text.strip().lower() not in ("排队", "queue")) else None
    with _pending_lock:
        cur = _pending.get(chat_id)
        already = bool(cur and cur.get("sid") == sid)
        if not already:
            # 卡片等待期间用户发的指令文字：选了排队就别丢，一并入队
            _pending[chat_id] = {"sid": sid, "file": session_file,
                                 "queue": [keep] if keep else [],
                                 "started": time.time()}
    if not already:
        threading.Thread(target=_wait_and_drain, args=(chat_id,), daemon=True).start()
    send_text(chat_id,
              f"⏳ 已进入排队接管（{tag}）：\n"
              "· 现在发的消息会排队，等它回合结束自动依次执行\n"
              "· /peek 看它当前进度，/unqueue 取消排队（保留绑定）\n"
              "· 等待上限 30 分钟，超时自动放弃")


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


def _send_card(chat_id: str, card: dict):
    """发送交互卡片，成功返回 message_id（卡片定格更新用），失败返回 None。"""
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        ).build()
    try:
        resp = api.im.v1.message.create(req)
        if not resp.success():
            print(f"[发卡片失败] code={resp.code} msg={resp.msg}")
            return None
        return resp.data.message_id if resp.data else None
    except Exception as e:
        print(f"[发卡片异常] {e!r}")
        return None


def send_panel(chat_id: str) -> None:
    """发送快捷命令卡片：每条命令一个按钮，点击即发送对应消息（卡片回传交互）。"""
    if not _commands:
        send_text(chat_id, "还没有快捷命令。用法：/addcmd 标签 消息内容\n"
                           "例：/addcmd 状态 /status\n添加后发 /panel 生成按钮面板。")
        return
    rows, row = [], []
    for i, c in enumerate(_commands):
        row.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": c.get("label", "?")},
            "type": "default",
            "value": {"i": str(i)},
        })
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": "⚡ 快捷命令"}},
        "elements": [{"tag": "action", "actions": r} for r in rows],
    }
    if not _send_card(chat_id, card):
        send_text(chat_id, "❌ 卡片发送失败，重试 /panel。")


# ---------------- 待决策卡片（权限确认 / 接管选择等，支持按钮+文字自定义回复） ----------------
_choices_lock = threading.Lock()
_choices: dict = {}   # chat_id -> {"id","event","answer","tag","labels"}


def _freeze_choice_card(msg_id, card: dict, answer) -> None:
    """用户已答复（点按钮/回文字/超时/取消）：把卡片更新成定格状态。

    在选项上直接体现：选中项变主色并标（已选择），其余按钮置灰不可点；
    文字/超时/取消则在卡片尾部附一行说明。更新失败只打日志不影响流程。
    """
    if not msg_id:
        return
    try:
        new_card = json.loads(json.dumps(card))
        chosen = answer[1] if answer and answer[0] == "choice" else None
        status = None
        if answer is None or answer[0] == "timeout":
            status = "⏰ 超时未选择（已按默认规则处理）"
        elif answer[0] == "text":
            status = f"💬 自定义回复：{(answer[1] or '')[:80]}"
        elif answer[0] == "cancel":
            status = "↩️ 已取消（任务结束）"
        for el in new_card.get("elements", []):
            if el.get("tag") != "action":
                continue
            for btn in el.get("actions", []):
                btn["disabled"] = True
                if chosen is not None and (btn.get("value") or {}).get("v") == chosen:
                    btn["type"] = "primary"
                    label = (btn.get("text") or {}).get("content", "?")
                    btn["text"] = {"tag": "plain_text", "content": f"{label}（已选择）"}
        if status:
            new_card.setdefault("elements", []).append(
                {"tag": "div", "text": {"tag": "lark_md", "content": status}})
        req = PatchMessageRequest.builder() \
            .message_id(msg_id) \
            .request_body(PatchMessageRequestBody.builder()
                          .content(json.dumps(new_card, ensure_ascii=False))
                          .build()) \
            .build()
        resp = api.im.v1.message.patch(req)
        if not resp.success():
            print(f"[卡片定格失败] code={resp.code} msg={resp.msg}")
    except Exception as e:
        print(f"[卡片定格异常] {e!r}")


def _ask_choice(chat_id: str, title: str, body_md: str, options: list,
                timeout: float, note: str = "", template: str = "orange",
                tag: str = "") -> tuple:
    """发一张选择卡片并阻塞等用户响应（点按钮，或直接回文字=自定义输入）。

    必须在工作线程里调用（会阻塞到用户响应或超时，不能占用 WS 事件循环）。
    options: [{"label": 按钮文字, "value": 选项值, "style": "primary"/"danger"/"default"}]
    返回 ("choice", value) / ("text", 用户原文) / ("timeout", None) / ("cancel", None)。
    同一聊天同时只挂一个待决策：新的会顶掉旧的（旧的按超时返回）。
    答复后卡片会更新成定格状态（选中项标注、按钮禁用）。
    卡片发送失败时自动降级为文字提问，文字回复路径不依赖卡片回传订阅。
    """
    req_id = f"c{time.time_ns()}"
    entry = {"id": req_id, "event": threading.Event(), "answer": None,
             "tag": tag, "labels": {o.get("value"): o.get("label", "?") for o in options}}
    with _choices_lock:
        old = _choices.get(chat_id)
        if old is not None:
            old["answer"] = ("timeout", None)
            old["event"].set()
        _choices[chat_id] = entry
    btns = [{"tag": "button", "text": {"tag": "plain_text", "content": o.get("label", "?")},
             "type": o.get("style", "default"), "value": {"ask": req_id, "v": o.get("value")}}
            for o in options]
    elements = []
    if body_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body_md}})
    elements.append({"tag": "action", "actions": btns})
    if note:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": note}})
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}
    msg_id = _send_card(chat_id, card)
    if not msg_id:
        labels = " / ".join(o.get("label", "?") for o in options)
        send_text(chat_id, f"💬 {title}\n{body_md}\n请回复选择：{labels}"
                           + (f"\n{note}" if note else ""))
    entry["event"].wait(timeout)
    with _choices_lock:
        if _choices.get(chat_id) is entry:
            _choices.pop(chat_id, None)
    answer = entry["answer"] if entry["answer"] is not None else ("timeout", None)
    _freeze_choice_card(msg_id, card, answer)
    return answer


def _resolve_choice(chat_id: str, req_id: str, val):
    """卡片按钮回调：把选项写回等待线程。返回按钮文字（给 toast），已答/过期返回 None。"""
    with _choices_lock:
        entry = _choices.get(chat_id)
        if entry is None or entry.get("id") != req_id or entry["answer"] is not None:
            return None
        entry["answer"] = ("choice", val)
        entry["event"].set()
        return entry["labels"].get(val, str(val))


def _cancel_perm_choice(chat_id: str) -> None:
    """claude 进程已退出（跑完/被 /stop）：权限确认等不下去了，标记取消。"""
    with _choices_lock:
        entry = _choices.get(chat_id)
        if entry is not None and entry.get("tag") == "perm" and entry["answer"] is None:
            entry["answer"] = ("cancel", None)
            entry["event"].set()


# 权限确认的自定义文字 → 决定（其余文字按"拒绝并带说明"处理，说明会转给 Claude）
PERM_ALLOW_WORDS = {"y", "yes", "ok", "1", "允许", "同意", "好", "可以", "准许", "allow", "approve"}
PERM_DENY_WORDS = {"n", "no", "2", "拒绝", "不行", "不可以", "不准", "deny", "reject", "取消"}


def _safe_md(s: str) -> str:
    """Strip lone surrogates (text corrupted by some hosts' broken encoding)."""
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _perm_input_display(tool: str, inp) -> str:
    """权限确认卡片上展示的操作内容：命令/路径优先，其余压缩成 JSON。"""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "url", "pattern", "query"):
            if inp.get(key):
                return _safe_md(str(inp[key]))[:600]
    return _safe_md(json.dumps(inp, ensure_ascii=False))[:600]


def _perm_server() -> None:
    """本地 TCP 通道：perm_mcp.py（claude 的权限回调 MCP 服务器）把请求转过来。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", PERM_PORT))
    except OSError as e:
        print(f"[权限通道] 端口 {PERM_PORT} 绑定失败：{e!r}（权限确认将自动拒绝，其余功能不受影响）")
        return
    srv.listen(8)
    print(f"[权限通道] 127.0.0.1:{PERM_PORT} 已监听")
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=_perm_conn, args=(conn,), daemon=True).start()


def _perm_conn(conn) -> None:
    """一次连接 = 一次权限确认：读请求 → 发卡片等用户 → 把决定写回连接。"""
    try:
        conn.settimeout(30)
        line = conn.makefile("rb").readline()
        payload = json.loads(line) if line.strip() else None
    except Exception:
        payload = None
    result = {"behavior": "deny", "message": "权限确认请求无效"}
    try:
        if payload and payload.get("type") == "perm" and payload.get("chat_id"):
            chat_id = payload["chat_id"]
            tool = str(payload.get("tool_name") or "?")
            disp = _perm_input_display(tool, payload.get("tool_input"))
            kind, val = _ask_choice(
                chat_id, "🔐 需要权限确认", f"**工具**：`{tool}`\n**内容**：\n{disp}",
                [{"label": "✅ 允许", "value": "allow", "style": "primary"},
                 {"label": "❌ 拒绝", "value": "deny", "style": "danger"}],
                timeout=PERM_TIMEOUT, template="orange", tag="perm",
                note="也可以直接回复文字：`允许` / `拒绝`，或任意说明（将作为拒绝原因转给 Claude）")
            if kind == "choice":
                if val == "allow":
                    result = {"behavior": "allow"}
                else:
                    result = {"behavior": "deny", "message": "用户在飞书上拒绝了本次操作"}
            elif kind == "text":
                t = (val or "").strip().lower()
                if t in PERM_ALLOW_WORDS:
                    result = {"behavior": "allow"}
                elif t in PERM_DENY_WORDS:
                    result = {"behavior": "deny", "message": "用户在飞书上拒绝了本次操作"}
                else:
                    result = {"behavior": "deny", "message": f"用户回复：{val}"}
            else:
                if kind == "cancel":
                    result = {"behavior": "deny", "message": "任务已结束，权限确认已取消"}
                else:
                    result = {"behavior": "deny", "message": "⏰ 等待确认超时，已按拒绝处理"}
                    send_text(chat_id, f"⏰ 权限确认超时（{tool}），已按拒绝处理，"
                                       f"可用 /mode 调整权限模式后重发。")
        elif payload and payload.get("type") == "perm":
            result = {"behavior": "deny", "message": "权限确认缺少聊天上下文"}
    except Exception as e:
        print(f"[权限确认处理出错] {e!r}")
        result = {"behavior": "deny", "message": "权限确认处理内部错误"}
    finally:
        try:
            conn.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


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


def shell_task(chat_id: str, command: str) -> None:
    """飞书直通 shell：!command 在本机 PowerShell 直接执行（不经过 Claude）。

    相当于桌面终端的 bash 模式；白名单用户本就能通过 Claude 执行任意命令，
    这里只是省掉模型中转。注意会绕过 Claude 的权限模式，输出用 UTF-8 读取。
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command],
            cwd=str(_chat_work_dir(chat_id)), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=SHELL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        send_text(chat_id, f"⏱ 命令超过 {SHELL_TIMEOUT}s 被中断（长任务建议发给 Claude 处理）")
        return
    except FileNotFoundError:
        send_text(chat_id, "❌ 找不到 powershell。")
        return
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    body = out
    if err:
        body = (out + "\n" if out else "") + "[stderr]\n" + err
    if not body:
        body = "（无输出）"
    send_text(chat_id, f"$ {command}\n{body}\n[exit {r.returncode}]")


def run_task(chat_id: str, prompt: str) -> None:
    """任务入口：同一会话同时只跑一个；忙时新消息排队，跑完自动依次执行。

    像桌面终端的 Claude 一样：执行中发消息不会丢，当前回合思考完就轮到它。
    互斥键是会话（见 _task_key）：不同会话互不阻塞。
    """
    key = _task_key(chat_id)
    with _running_lock:
        if key in _running:
            holder = _running[key]
            if holder.get("chat_id") != chat_id:
                send_text(chat_id, f"⚠️ 会话 {key[:8]}… 正在被另一个聊天使用，"
                                   "为避免上下文交错已拒绝。稍后重试。")
                return
            with _msg_queue_lock:
                q = _msg_queue.setdefault(key, [])
                q.append(prompt)
                n = len(q)
            send_text(chat_id, f"📥 已排队（第 {n} 条），当前任务完成后自动执行")
            return
        slot = {"chat_id": chat_id, "proc": None, "started": time.time(), "reason": None}
        _running[key] = slot
    try:
        while True:
            _run_claude(chat_id, prompt, slot)
            if slot.get("reason") in ("stop", "timeout"):
                # 被中断/超时：排队的消息一并取消——用户按停时预期是全停
                with _msg_queue_lock:
                    dropped = _msg_queue.pop(key, [])
                if dropped:
                    send_text(chat_id, f"🚫 排队的 {len(dropped)} 条消息已一并取消。")
                return
            with _msg_queue_lock:
                q = _msg_queue.get(key)
                prompt = q.pop(0) if q else None
                if not q:
                    _msg_queue.pop(key, None)
            if prompt is None:
                return
            slot["reason"] = None
            slot["proc"] = None
            slot["started"] = time.time()
    except Exception as e:
        send_text(chat_id, f"❌ 桥接器内部错误：{e!r}")
    finally:
        with _running_lock:
            if _running.get(key) is slot:
                _running.pop(key, None)


def _run_claude(chat_id: str, prompt: str, slot: dict, ack: bool = True) -> None:
    """调用 claude -p（stream-json），实时转发输出。"""
    session_id = _lookup_session(chat_id)
    if ack:
        label = _chat_label(chat_id)
        tag = f"续接 {label} · {_mode_label()}" if label else f"新会话 · {_mode_label()}"
        send_text(chat_id, f"🤖 收到（{tag}），开始执行…")

    # 注意：提示词通过 stdin 传入，避免经 .cmd 垫片时被 cmd.exe 解析特殊字符
    cmd = [CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose"]
    if PERMISSION_MODE:
        cmd += ["--permission-mode", PERMISSION_MODE]
    # 权限确认走飞书卡片：claude 遇到权限提示时调用 MCP 工具
    # （perm_mcp.py → 本地 socket → 卡片/文字回复）。bypassPermissions 不会产生提示，跳过。
    if PERMISSION_MODE != "bypassPermissions":
        mcp_config = {"mcpServers": {"feishu": {
            "command": sys.executable, "args": [str(BASE_DIR / "perm_mcp.py")]}}}
        cmd += ["--permission-prompt-tool", "mcp__feishu__request_permission",
                "--mcp-config", json.dumps(mcp_config, separators=(",", ":"))]
    if CLAUDE_EXTRA_ARGS:
        cmd += shlex.split(CLAUDE_EXTRA_ARGS)
    if session_id:
        cmd += ["--resume", session_id]

    # FEISHU_BOT_CHILD=1：让 notify.py 跳过机器人自己的 claude 子进程，避免重复通知
    # FEISHU_CHAT_ID/FEISHU_PERM_PORT：perm_mcp.py 借此路由卡片和连回本进程
    child_env = {**os.environ, "FEISHU_BOT_CHILD": "1",
                 "FEISHU_CHAT_ID": chat_id, "FEISHU_PERM_PORT": str(PERM_PORT)}
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(_chat_work_dir(chat_id)), env=child_env,
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

    # 超时看门狗（CLAUDE_TIMEOUT<=0 时不限时，挂死用 /stop 远程杀）
    timer = None
    if CLAUDE_TIMEOUT > 0:
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
        if timer:
            timer.cancel()
    returncode = proc.wait()
    _cancel_perm_choice(chat_id)   # claude 已退出（跑完/被停），挂着的权限确认不再等
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
                "work_dir": str(_chat_work_dir(chat_id)),
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
        save_sessions()

    final_text = (result.get("result") or "").strip()
    if final_text and final_text != last_text:
        send_text(chat_id, final_text)

    dur = (result.get("duration_ms") or 0) / 1000
    cost = result.get("total_cost_usd") or 0
    turns = result.get("num_turns", "?")
    label = _chat_label(chat_id)
    line = f"✅ {label + ' ' if label else ''}完成（{_mode_label()}） · 耗时 {dur:.1f}s · {turns} 轮对话"
    if cost:
        line += f" · ${cost:.3f}"
    if result.get("is_error"):
        line = "⚠️ 任务异常结束 · " + line
    send_text(chat_id, line)

    denials = result.get("permission_denials") or []
    if denials:
        send_text(chat_id, f"⚠️ 有 {len(denials)} 个操作因权限被拒绝。"
                           "如需放开：发 /mode bypassPermissions（机器人级设置，注意风险），"
                           "或把具体命令加进 Claude 设置的 allow 列表。")


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


def _session_state(session_file: Path):
    """看转录尾部判断会话当前状态，返回 (state, age)。

    state：'running'=回合进行中（不可安全接管）；'idle'=回合已闭合（可安全接管）；
    读不到返回 (None, None)。age 是文件距上次写入的秒数。
    判定依据——最后一条实质对话记录：
      · assistant 且含 text 块（正文回复）→ 回合已闭合 = idle
      · 悬空 user，或只含 tool_use 的 assistant（工具执行中），或空 content 的
        assistant（流式开始标记）→ 回合进行中 = running
      · 悬空但文件太久没动（user 5 分钟 / tool_use 15 分钟）→ 大概率已异常结束，按 idle
    （注意转录里 tool_result 记录的 type 也是 "user"，需按内容形态排除。）
    """
    try:
        st = session_file.stat()
        age = time.time() - st.st_mtime
        with session_file.open("rb") as fh:
            fh.seek(max(0, st.st_size - 262144))  # 只看尾部 256KB
            data = fh.read().decode("utf-8", "replace")
        last = ""
        for line in data.splitlines():
            if '"type"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            c = (d.get("message") or {}).get("content")
            if t == "user":
                if d.get("isMeta"):
                    continue
                if isinstance(c, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                    continue  # 工具结果记录，不是对话输入
                has_text = (isinstance(c, str) and c.strip()) or (
                    isinstance(c, list) and any(
                        isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                        for b in c))
                if has_text:
                    last = "user"
            elif isinstance(c, list):
                if any(isinstance(b, dict) and b.get("type") == "text"
                       and (b.get("text") or "").strip() for b in c):
                    last = "assistant"            # 有正文 = 回合闭合
                elif any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c):
                    last = "assistant_tool"       # 只有工具调用 = 工具还在执行
        if last == "assistant":
            return "idle", age
        if last == "user":
            return ("running", age) if age <= 300 else ("idle", age)
        if last == "assistant_tool":
            return ("running", age) if age <= 900 else ("idle", age)
        return None, age
    except OSError:
        return None, None


def _session_tail(session_file: Path, n: int = 5):
    """转录尾部最近的对话动态 [(时间, 角色, 摘要)]，用于 /peek 旁观。"""
    out = []
    try:
        st = session_file.stat()
        with session_file.open("rb") as fh:
            fh.seek(max(0, st.st_size - 262144))
            data = fh.read().decode("utf-8", "replace")
        for line in data.splitlines():
            if '"type"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            ts = (d.get("timestamp") or "")[11:19]
            c = (d.get("message") or {}).get("content")
            if t == "user":
                if d.get("isMeta"):
                    continue
                txt = ""
                if isinstance(c, str):
                    txt = c.strip()
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                            txt = b["text"].strip()
                            break
                if txt:
                    out.append((ts, "👤", txt.replace("\n", " ")[:60]))
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        out.append((ts, "🤖", b["text"].strip().replace("\n", " ")[:60]))
                    elif b.get("type") == "tool_use":
                        out.append((ts, "🔧", _tool_summary(b.get("name", ""), b.get("input"))))
        return out[-n:]
    except OSError:
        return []


# ---------------- 命令处理 ----------------
def handle_command(chat_id: str, text: str) -> None:
    global WORK_DIR, PERMISSION_MODE
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
        with _pending_lock:
            had_pending = _pending.pop(chat_id, None)
        if had_pending:
            send_text(chat_id, f"↩️ 已取消排队接管（{len(had_pending['queue'])} 条消息被丢弃）。")
            return
        with _running_lock:
            slots = [(k, v) for k, v in _running.items() if v.get("chat_id") == chat_id]
        if slots:
            dropped_total = 0
            for key, slot in slots:
                slot["reason"] = "stop"
                if slot.get("proc"):
                    kill_task(slot["proc"])
                with _msg_queue_lock:
                    dropped_total += len(_msg_queue.pop(key, []))
            extra = f"，排队的 {dropped_total} 条消息将一并取消" if dropped_total else ""
            send_text(chat_id, f"🛑 正在中断当前任务{extra}…")
        else:
            send_text(chat_id, "当前没有正在执行的任务。")
    elif cmd == "/status":
        with _running_lock:
            slots = [(k, v) for k, v in _running.items() if v.get("chat_id") == chat_id]
        with _msg_queue_lock:
            qn = len(_msg_queue.get(_task_key(chat_id)) or [])
        with _pending_lock:
            pend = _pending.get(chat_id)
        if slots:
            msg = "⏳ 任务执行中" + ("（多会话并行）" if len(slots) > 1 else "") + "：" + \
                  "、".join(f"会话 {k[:8]}… {int(time.time() - v['started'])}s" for k, v in slots) + \
                  "\n（/stop 可全部中断）"
        else:
            msg = "空闲中。"
        if qn:
            msg += f"\n📥 排队中：{qn} 条消息"
        if pend:
            msg += f"\n⏳ 等待会话 {pend['sid'][:8]}… 空闲（已排 {len(pend['queue'])} 条）"
        send_text(chat_id, msg)
    elif cmd == "/session":
        sess = _sessions.get(chat_id) or {}
        sid = sess.get("session_id")
        if sid:
            name = _names.get(sid)
            show = f"{sid}" + (f"「{name}」" if name else "") + \
                   f"\n（在本机执行 claude --resume {sid} 可接续此会话）"
        else:
            show = "无"
        send_text(chat_id, f"当前会话：{show}\n工作目录：{_chat_work_dir(chat_id)}")
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
        # 活跃检测：回合还在跑 → 发卡片选 排队/强制/取消（60 秒无响应默认排队）
        session_file, _ = _find_session_file(sid)
        if session_file and not force:
            state, age = _session_state(session_file)
            if state == "running":
                threading.Thread(target=_takeover_running_dialog,
                                 args=(chat_id, sid, cwd, session_file, age),
                                 daemon=True).start()
                return
        _bind_session(chat_id, sid, cwd)
        send_text(chat_id,
                  f"✅ 已接管会话 {sid[:8]}…（目录：{_chat_work_dir(chat_id)}）\n"
                  "下一条消息将带上该会话的全部上下文。\n"
                  "⚠️ 若桌面那边还开着，请先退出（避免两边同时写入）。")
    elif cmd == "/peek":
        want = arg.strip()
        sid = None
        if not want:
            sid = (_sessions.get(chat_id) or {}).get("session_id")
            if not sid:
                send_text(chat_id, "当前聊天没有绑定会话。用法：/peek <序号|会话ID|latest>")
                return
        elif want.lower() == "latest":
            sid, _ = _latest_desktop_session()
        elif want.isdigit():
            items = _recent_sessions(8, include_managed=True)
            idx = int(want) - 1
            if 0 <= idx < len(items):
                sid = items[idx]["session_id"]
        else:
            sid, _ = _find_session_file(want)
        if not sid:
            send_text(chat_id, f"❌ 找不到会话 {want}")
            return
        sf, cwd2 = _find_session_file(sid)
        if not sf:
            send_text(chat_id, f"❌ 找不到会话 {sid[:8]}… 的转录文件")
            return
        state, age = _session_state(sf)
        proj = Path(cwd2).name if cwd2 else "?"
        name = _names.get(sid)
        if state == "running":
            head = f"👀 [{proj}] {sid[:8]}… ⚡回合进行中（{int(age)} 秒前仍在写入）"
        elif state == "idle":
            head = f"👀 [{proj}] {sid[:8]}… 💤空闲（最后活动 {_rel_time(sf.stat().st_mtime)}）"
        else:
            head = f"👀 [{proj}] {sid[:8]}…（状态未知）"
        if name:
            head += f" 📛{name}"
        lines = [head, "最近动态："] + [f"  {t} {who} {txt}" for t, who, txt in _session_tail(sf)]
        lines.append(f"\n/takeover {sid} ← 接管（正在跑的会话会自动排队，跑完接续）")
        send_text(chat_id, "\n".join(lines))
    elif cmd == "/unqueue":
        with _msg_queue_lock:
            msgs = _msg_queue.pop(chat_id, [])
        with _pending_lock:
            pend = _pending.pop(chat_id, None)
        parts = []
        if msgs:
            parts.append(f"消息队列 {len(msgs)} 条")
        if pend:
            parts.append(f"排队接管（{len(pend['queue'])} 条）")
        if parts:
            send_text(chat_id, "↩️ 已清空：" + "、".join(parts) +
                               "。正在执行的任务不受影响（要中断用 /stop）。")
        else:
            send_text(chat_id, "当前没有排队中的消息。")
    elif cmd == "/addcmd":
        parts_arg = arg.split(None, 1)
        if len(parts_arg) < 2 or not parts_arg[1].strip():
            send_text(chat_id, "用法：/addcmd 按钮标签 消息内容\n"
                               "例：/addcmd 状态 /status\n"
                               "/addcmd 看进度 /peek latest\n"
                               "/addcmd 测试 跑一下测试套件并汇总失败原因")
            return
        with _commands_lock:
            _commands.append({"label": parts_arg[0][:12], "text": parts_arg[1].strip()[:500]})
        save_commands()
        send_text(chat_id, f"✅ 已添加「{parts_arg[0][:12]}」。发 /panel 生成按钮面板"
                           "（面板是快照，加删命令后要重发 /panel）。")
    elif cmd == "/delcmd":
        if not arg.isdigit() or not (1 <= int(arg) <= len(_commands)):
            send_text(chat_id, "用法：/delcmd 序号（用 /cmds 查看当前命令）")
            return
        with _commands_lock:
            removed = _commands.pop(int(arg) - 1)
        save_commands()
        send_text(chat_id, f"✅ 已删除「{removed.get('label')}」。记得重发 /panel。")
    elif cmd == "/cmds":
        if not _commands:
            send_text(chat_id, "还没有快捷命令（/addcmd 添加）。")
            return
        lines = ["⚡ 当前快捷命令："] + [
            f"{i}. {c['label']} → {c['text'][:40]}" for i, c in enumerate(_commands, 1)]
        send_text(chat_id, "\n".join(lines))
    elif cmd == "/panel":
        send_panel(chat_id)
    elif cmd == "/listen":
        if _chat_types.get(chat_id) != "group":
            send_text(chat_id, "单聊本来就无需 @，这个设置只对群聊有意义。")
            return
        if chat_id in _listen:
            with _listen_lock:
                _listen.discard(chat_id)
            save_listen()
            send_text(chat_id, "↩️ 已关闭：本群恢复需要 @机器人 才响应。")
        else:
            with _listen_lock:
                _listen.add(chat_id)
            save_listen()
            label = _chat_label(chat_id)
            send_text(chat_id,
                      "📢 本群已进入「免 @ 直连模式」\n"
                      "· 后续所有消息都会被接收，无需再 @机器人\n"
                      "· 普通文字 = 给 Claude 的指令；/开头 = 机器人命令；!开头 = 本机直通命令\n"
                      "· 再发一次 /listen 即可退出（恢复 @ 触发）\n"
                      "· 非白名单成员的消息仍会被拒绝，人多的群建议保持 @ 模式\n"
                      f"· 当前绑定：{label or '（暂无会话，发首条消息自动新建）'}")
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
            send_text(chat_id, f"当前工作目录:{_chat_work_dir(chat_id)}\n用法:/cwd D:\\path\\to\\project")
        else:
            p = Path(arg)
            if not p.is_dir():
                send_text(chat_id, f"❌ 目录不存在：{p}")
            else:
                WORK_DIR = p.resolve()   # 改全局默认
                with _sessions_lock:
                    _sessions.pop(chat_id, None)  # 只重置本聊天的会话，不影响其他聊天
                save_sessions()
                send_text(chat_id, f"📁 工作目录已切换：{WORK_DIR}\n（本聊天的历史会话已重置）")
    elif cmd == "/mode":
        if not arg:
            send_text(chat_id, f"当前权限模式：{PERMISSION_MODE or 'default'}\n"
                               f"切换：/mode <{' | '.join(sorted(PERMISSION_MODES))}>\n"
                               "对下一条消息生效（机器人级设置，对所有聊天生效）。")
        elif arg in PERMISSION_MODES:
            PERMISSION_MODE = arg
            warn = "\n⚠️ bypassPermissions = 白名单里的人可让 Claude 做任何事，风险自负。" \
                if arg == "bypassPermissions" else ""
            send_text(chat_id, f"✅ 权限模式已切换：{arg}（下一条消息生效）{warn}")
        else:
            send_text(chat_id, f"❌ 无效模式 {arg}\n可选：{' | '.join(sorted(PERMISSION_MODES))}")
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

        try:
            text = json.loads(msg.content or "{}").get("text", "")
        except json.JSONDecodeError:
            return

        # 群聊：默认需 @机器人（白名单负责拦截外人）；/listen 开启的群免 @ 全量接收
        _chat_types[chat_id] = msg.chat_type
        if msg.chat_type == "group":
            with _listen_lock:
                listen = chat_id in _listen
            if not (msg.mentions or listen):
                # 白名单用户发了裸命令（没 @）：提示怎么触发/怎么开免 @（10 分钟限频防刷屏）
                if text.startswith("/") and sender_id in ALLOWED_USERS \
                        and time.time() - _hint_ts.get(chat_id, 0) > 600:
                    _hint_ts[chat_id] = time.time()
                    send_text(chat_id, "（群聊里需要 @机器人 才会响应，如 @机器人 /status。\n"
                                       "只有你+机器人的群可以 @机器人 /listen 开启免 @ 直连模式，"
                                       "之后所有消息直接处理）")
                return
            for m in msg.mentions or []:
                text = text.replace(m.key or "", "")
            text = text.strip()
            if not text:
                return

        text = text.strip()
        if not text:
            return
        dispatch_text(chat_id, text)
    except Exception as e:
        print(f"[处理消息出错] {e!r}")


def dispatch_text(chat_id: str, text: str, sender_id: str = "") -> None:
    """统一文本分发（消息与卡片按钮共用）：命令 / !shell / 排队接管 / Claude 任务。

    调用方负责白名单校验；按钮回调与 on_message 都走这里。
    """
    # 记录通知目标：桌面 hook 的 notify.py 读它把提醒推到这个聊天（最后活跃的聊天优先）
    if sender_id:
        try:
            (BASE_DIR / "notify_target.json").write_text(
                json.dumps({"chat_id": chat_id, "open_id": sender_id,
                            "updated": datetime.now().isoformat(timespec="seconds")},
                           ensure_ascii=False), "utf-8")
        except OSError:
            pass

    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    # !command：直通 shell（相当于桌面终端的 bash 模式），不经 Claude、不排队
    if text.startswith("!") and text[1:].strip():
        shell_cmd = text[1:].strip()
        threading.Thread(target=shell_task, args=(chat_id, shell_cmd), daemon=True).start()
        return

    # 有待决策卡片（权限确认/接管选择）：纯文字 = 自定义回复（命令和 ! 前缀不受影响）
    routed = False
    with _choices_lock:
        entry = _choices.get(chat_id)
        if entry is not None and entry["answer"] is None:
            entry["answer"] = ("text", text)
            entry["event"].set()
            routed = True
    if routed:
        return

    # 排队接管中：消息入队，等目标会话回合结束后由 _wait_and_drain 自动执行
    with _pending_lock:
        pend = _pending.get(chat_id)
        if pend is not None:
            pend["queue"].append(text)
            queued = len(pend["queue"])
    if pend is not None:
        send_text(chat_id, f"📥 已排队（第 {queued} 条），等会话回合结束自动执行。/peek 看进度，/unqueue 取消。")
        return

    threading.Thread(target=run_task, args=(chat_id, text), daemon=True).start()


def _card_toast(content: str, type_: str = "success") -> "P2CardActionTriggerResponse":
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    return P2CardActionTriggerResponse({"toast": {"type": type_, "content": content}})


def on_app_version(event) -> None:
    """应用版本事件回调：发布/审核结果 → 通知到通知目标聊天（订阅、权限变更从此刻生效）。"""
    try:
        data = event.event
        if data is None:
            return
        version = data.version_id or "?"
        operation = data.operation or ""
        remark = (data.remark or "").strip()
        if "pass" in operation.lower() or "通过" in operation:
            text = (f"🎉 飞书应用新版本已发布生效：{version}\n"
                    f"（本次版本里的订阅/权限变更现已生效）")
        else:
            text = f"📋 应用版本审核事件：{operation} {version}\n{remark[:200]}"
        try:
            chat_id = json.loads((BASE_DIR / "notify_target.json").read_text("utf-8"))["chat_id"]
            send_text(chat_id, text)
        except Exception:
            print(f"[版本通知] 无通知目标，未推送：{text}")
    except Exception as e:
        print(f"[版本事件处理出错] {e!r}")


def on_card_action(event) -> object:
    """卡片按钮回调：点击快捷命令面板上的按钮 = 发送对应消息。"""
    try:
        data = event.event
        if data is None or data.context is None:
            return _card_toast("❌ 事件数据不完整", "error")
        chat_id = data.context.open_chat_id or ""
        op = data.operator.open_id if data.operator else ""
        if not chat_id:
            return _card_toast("❌ 缺少聊天信息", "error")
        if ALLOWED_USERS and op not in ALLOWED_USERS:
            return _card_toast("🔒 你不在白名单内", "error")
        value = ((data.action.value or {}) if data.action else {}) or {}
        req = value.get("ask")
        if req:
            label = _resolve_choice(chat_id, str(req), value.get("v"))
            if label is None:
                return _card_toast("❌ 该问题已回答或已过期", "error")
            return _card_toast(f"✅ 已选择：{label}")
        idx = value.get("i")
        if idx is None or not str(idx).isdigit() or int(idx) >= len(_commands):
            return _card_toast("❌ 该命令已删除，重新发 /panel 获取新面板", "error")
        text = _commands[int(idx)].get("text", "")
        if not text:
            return _card_toast("❌ 空命令", "error")
        threading.Thread(target=dispatch_text, args=(chat_id, text, op), daemon=True).start()
        return _card_toast(f"✅ 已发送：{text[:30]}")
    except Exception as e:
        return _card_toast(f"❌ {e!r}", "error")


def main() -> None:
    if not CLAUDE_PATH:
        sys.exit("❌ 找不到 claude 命令。请确认已安装 Claude Code 并在 PATH 中，"
                 "或在 .env 中设置 CLAUDE_PATH=claude 的完整路径")
    if not WORK_DIR.is_dir():
        sys.exit(f"❌ 工作目录不存在：{WORK_DIR}")
    valid_modes = PERMISSION_MODES | {""}
    if PERMISSION_MODE not in valid_modes:
        sys.exit(f"❌ CLAUDE_PERMISSION_MODE 无效：{PERMISSION_MODE}\n"
                 f"   可选值：{', '.join(sorted(PERMISSION_MODES))}")

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_card_action_trigger(on_card_action) \
        .register_p2_application_application_app_version_audit_v6(on_app_version) \
        .build()

    ws = lark.ws.Client(APP_ID, APP_SECRET,
                        event_handler=handler,
                        log_level=lark.LogLevel.INFO,
                        auto_reconnect=True)

    print("✅ 飞书机器人已启动（WebSocket 长连接，无需公网 IP）")
    print(f"   Claude：{CLAUDE_PATH}")
    print(f"   工作目录：{WORK_DIR}")
    print(f"   权限模式：{PERMISSION_MODE or 'default'}｜超时：{CLAUDE_TIMEOUT if CLAUDE_TIMEOUT > 0 else '不限时'}｜转发工具调用：{'开' if FORWARD_TOOLS else '关'}")
    print(f"   白名单：{len(ALLOWED_USERS)} 人" + ("" if ALLOWED_USERS else "（未配置，首条消息会提示如何配置）"))
    # 权限确认通道：perm_mcp.py（claude 的 MCP 权限回调）连回本进程
    threading.Thread(target=_perm_server, daemon=True).start()
    print("   等待消息中…（Ctrl+C 退出）")
    ws.start()  # 阻塞，断线自动重连


if __name__ == "__main__":
    main()
