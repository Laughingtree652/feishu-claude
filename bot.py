#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 ↔ 本地 Claude Code 桥接器（WebSocket 长连接，无需公网 IP）

飞书发消息给机器人 → 调用本地 claude CLI（多轮会话自动续接）→
Claude 的回复与工具调用实时转发回飞书，实现远程操控。

可用命令：/help /new /exit /handover /stop /status /session /sessions /takeover /cwd /mode /mute /unmute /provider
（会话独占：同一会话只归一个聊天，后接管者优先，被挤掉的聊天会收到通知）
"""
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import urllib.request
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 闲置回收的子进程检测用；未安装（没跑 uv sync）时自动退回纯时间判定
try:
    import psutil
except ImportError:
    psutil = None

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
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()  # 全局默认权限模式（/mode 可按聊天覆盖）
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "0"))   # 单次任务超时（秒），<=0 不限时
FORWARD_TOOLS = os.environ.get("FORWARD_TOOLS", "1").strip().lower() not in ("0", "false", "no")
CLAUDE_EXTRA_ARGS = os.environ.get("CLAUDE_EXTRA_ARGS", "").strip()
CLAUDE_PATH = os.environ.get("CLAUDE_PATH") or shutil.which("claude")


def _detect_git_bash() -> str:
    r"""定位 Git Bash 的 bash.exe（.env 显式指定 > git 安装位置反推 > PATH 兜底）。

    用途：Windows 上非交互 spawn 的 claude 默认拿 PowerShell 工具，settings 里
    Bash(git *) 等白名单规则全部不命中（每条命令都弹权限卡）。注入官方变量
    CLAUDE_CODE_GIT_BASH_PATH 可强制分身切回 Bash 工具——实测 v2.1.310 只认
    这个变量（SHELL/MSYSTEM/PATH 上有 bash 均无效），且 Git 装在非标准位置
    （如 D:\tools\Git）时 CLI 自身探测也找不到。"""
    if sys.platform != "win32":
        return ""
    manual = os.environ.get("GIT_BASH_PATH", "").strip()
    if manual:
        return manual
    git_exe = shutil.which("git")
    if git_exe:
        root = Path(git_exe).resolve().parent.parent   # git.exe 在 <根>\cmd 或 <根>\bin
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    bash_exe = shutil.which("bash")   # 兜底；System32\bash.exe 是 WSL 的，不能用
    if bash_exe and "system32" not in bash_exe.lower():
        return str(Path(bash_exe).resolve())
    return ""


GIT_BASH = _detect_git_bash()

# 权限模式全集（/mode 运行时切换 + 启动校验共用）
PERMISSION_MODES = {"default", "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
SHELL_TIMEOUT = 120  # 飞书 ! 命令直通 shell 的超时（秒）

# 权限确认卡片（--permission-prompt-tool → perm_mcp.py → 本地 socket → 飞书卡片）
PERM_PORT = int(os.environ.get("FEISHU_PERM_PORT", "37921"))     # 与 perm_mcp.py 约定的本地端口
PERM_TIMEOUT = int(os.environ.get("PERM_TIMEOUT_SECONDS", "0"))  # 等用户确认权限的上限（秒），0=不限时（与桌面终端一致）
PERM_GATE_WAIT = int(os.environ.get("PERM_GATE_WAIT", "300"))    # 桌面权限卡等待上限（秒）；超时不答复，回退桌面终端弹框
TAKEOVER_ASK_TIMEOUT = 60  # 接管运行中会话时等用户选卡片选项的上限（秒），超时默认排队

SESSION_FILE = BASE_DIR / "sessions.json"
MAX_CHUNK = 3500      # 单条飞书消息最大字符数，超长自动分段
SEND_INTERVAL = 0.5   # 发消息最小间隔（秒），避免触发飞书频控
LIVE_PATCH_INTERVAL = 1.2  # 流式卡片刷新间隔（秒）：更实时但太频繁会触发卡片更新频控

HELP_TEXT = """📖 命令列表
/help — 查看本帮助
/new — 新开一个零上下文的会话（旧会话历史保留，/takeover last 可接回）
/stop — 中断当前任务（排队的消息一并取消）
/status — 状态面板：机器人设置（权限/静默/免@）+ 会话绑定与进程工作状态
/session — 查看当前会话 ID（在电脑上 claude --resume <id> 可接续）
/sessions [条数] — 列出电脑上最近的会话（含当前绑定的；用过一段的自动带一行摘要）
/takeover [序号|会话ID|latest|last] — 接管电脑上的会话（latest=电脑最新；last=本聊天上次让出的；在跑的发卡片选：排队/强制/取消）
/exit — 退出当前接管的会话（解绑+回收驻留进程；历史保留，可再 /takeover）
/handover [说明] — 一键移交终端：中断当前活→分身收尾总结写进转录→自动退出，接手命令（claude --resume）进剪贴板，终端粘贴回车即续跑
/peek [序号|会话ID|latest|last] — 旁观会话当前进度（last=本聊天上次让出的会话）
/observe [序号|会话ID|latest|last] — 实时观察：转录有动静自动刷新快照卡片（last=上次让出的会话；观察中再 /ob 同一会话 = 重发新卡刷回底部）
/unobserve — 停止实时观察
/provider [序号|名称] — 查看/切换 Claude 供应商（cc-switch 库；切换即换 key，新起进程生效）
/unqueue — 取消排队接管
/rename [名字] — 给当前会话起名（/sessions 列表优先显示名字）
/cwd [路径] — 查看 / 切换 Claude 的工作目录
/mode [模式] — 查看 / 切换本聊天的权限模式（manual、auto、acceptEdits…，未设置时用全局默认）
/model [序号|名称|reset] — 查看/切换本聊天的模型（网关可用模型实时列出；reset 回退全局默认）
/listen — 群聊免 @ 模式：本群所有消息直接发给机器人（适合只有你+机器人的群）
/mute · /unmute — 暂停/恢复桌面事件推送（全局）
/quiet · /unquiet — 静默模式（本聊天）：不刷思考/工具过程，只推最终结果和权限确认

直接发送文本 = 给 Claude 下指令，多轮对话自动保持上下文。
回复以流式卡片实时呈现（边生成边更新，超长自动分段不丢内容）。
任务执行中发的消息会直接递给 Claude（工具间隙读取，边跑边回应，和桌面终端一致）。
Claude 需要权限确认时会发卡片，点按钮或直接回复文字（允许/拒绝/自定义说明）。
命令支持缩写：唯一前缀自动展开（如 /ob → /observe），歧义时提示候选。
!命令 = 直接在本机 PowerShell 执行（不经 Claude），如 !git status"""

# 卡片版帮助（/help 发送；HELP_TEXT 保留作卡片失败的兜底纯文本）
HELP_MD = """**🧩 会话管理**
- **/new** — 新开一个零上下文的会话（旧会话历史保留，/takeover last 可接回）
- **/stop** — 中断当前任务（排队的消息一并取消）
- **/exit** — 退出当前接管的会话（解绑+回收驻留进程；历史保留，可再 /takeover）
- **/handover** [说明] — 一键移交终端：中断当前活→分身收尾总结写进转录→自动退出，接手命令进剪贴板，终端粘贴回车即续跑

**📡 接管与进度**
- **/takeover** [序号|会话ID|latest|last] — 接管电脑上的会话（latest=电脑最新；last=本聊天上次让出的；在跑的发卡片选：排队/强制/取消）
- **/peek** [序号|会话ID|latest|last] — 旁观会话当前进度
- **/observe** [序号|会话ID|latest|last] — 实时观察：转录有动静自动刷新快照卡片
- **/unobserve** — 停止实时观察
- **/sessions** [条数] — 列出电脑上最近的会话（用过一段的自动带一行摘要）
- **/session** — 查看当前会话 ID（在本机 claude --resume 会话ID 可接续）
- **/rename** [名字] — 给当前会话起名（/sessions 列表优先显示名字）
- **/unqueue** — 取消排队接管

**⚙️ 设置**
- **/status** — 状态面板：机器人设置 + 会话绑定与进程工作状态
- **/mode** [模式] — 查看/切换本聊天的权限模式（manual、auto、acceptEdits…，未设置时用全局默认）
- **/model** [序号|名称|reset] — 查看/切换本聊天的模型（reset 回退全局默认）
- **/cwd** [路径] — 查看/切换 Claude 的工作目录
- **/provider** [序号|名称] — 查看/切换 Claude 供应商（cc-switch 库；切换即换 key，新起进程生效）
- **/listen** — 群聊免 @ 模式：本群所有消息直接发给机器人（适合只有你+机器人的群）
- **/mute · /unmute** — 暂停/恢复桌面事件推送（全局）
- **/quiet · /unquiet** — 静默模式（本聊天）：只推最终结果和权限确认

**💡 说明**
- 直接发送文本 = 给 Claude 下指令，多轮对话自动保持上下文
- 回复以流式卡片实时呈现；任务执行中发的消息会直接递给 Claude（边跑边回应）
- Claude 需要权限确认时会发卡片，点按钮或直接回复文字
- 命令支持唯一前缀缩写（如 /ob → /observe），歧义时提示候选
- **!命令** = 直接在本机 PowerShell 执行（不经 Claude），如 !git status"""

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

# ---------------- 聊天级模型设置（chat_id -> 模型名，/model 维护；分身 spawn 时 --model 显式指定） ----------------
MODELS_FILE = BASE_DIR / "models.json"
_models_lock = threading.Lock()
_chat_models: dict = {}
if MODELS_FILE.exists():
    try:
        _chat_models = json.loads(MODELS_FILE.read_text("utf-8"))
    except Exception:
        _chat_models = {}


def save_models() -> None:
    with _models_lock:
        try:
            MODELS_FILE.write_text(json.dumps(_chat_models, ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            pass


# ---------------- 会话摘要（session_id -> 一行总结，驻留进程收尾时后台生成） ----------------
SUMMARY_FILE = BASE_DIR / "summaries.json"
_summaries: dict = {}
if SUMMARY_FILE.exists():
    try:
        _summaries = json.loads(SUMMARY_FILE.read_text("utf-8"))
    except Exception:
        _summaries = {}


def save_summaries() -> None:
    try:
        SUMMARY_FILE.write_text(json.dumps(_summaries, ensure_ascii=False, indent=1), "utf-8")
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


# ---------------- 静默模式 / 权限模式覆盖（/quiet /mode 维护，均按聊天独立落盘） ----------------
# 静默模式：开着时本聊天不转发过程性输出（思考文本/🔧 工具调用），只推权限确认卡片、
# 最终结果和异常——通知只出现在"需要你拍板"的时刻
QUIET_FILE = BASE_DIR / "quiet.json"
_quiet: set = set()
if QUIET_FILE.exists():
    try:
        _quiet = set(json.loads(QUIET_FILE.read_text("utf-8")))
    except Exception:
        _quiet = set()
elif (BASE_DIR / "quiet.flag").exists():
    # 旧版全局静默开关迁移：对所有绑定过会话的聊天开启，并删除旧标记
    _quiet = set(_sessions)
    try:
        QUIET_FILE.write_text(json.dumps(sorted(_quiet), ensure_ascii=False, indent=1), "utf-8")
        (BASE_DIR / "quiet.flag").unlink()
    except OSError:
        pass

MODES_FILE = BASE_DIR / "modes.json"
_modes: dict = {}   # chat_id -> 权限模式（本聊天独立设置；未设置用全局默认 PERMISSION_MODE）
if MODES_FILE.exists():
    try:
        _modes = json.loads(MODES_FILE.read_text("utf-8"))
    except Exception:
        _modes = {}


def save_quiet() -> None:
    try:
        QUIET_FILE.write_text(json.dumps(sorted(_quiet), ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def save_modes() -> None:
    try:
        MODES_FILE.write_text(json.dumps(_modes, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def _chat_mode(chat_id: str) -> str:
    """本聊天生效的权限模式：聊天独立设置优先，否则全局默认。"""
    return _modes.get(chat_id) or PERMISSION_MODE or "default"


def save_sessions() -> None:
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, ensure_ascii=False, indent=1), "utf-8")
    _save_desktop_route()   # 绑定关系变化必经此处，路由文件随最新鲜


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


def _mode_label(chat_id: str) -> str:
    """本聊天权限模式的简短标识（bypass 加警示）。"""
    m = _chat_mode(chat_id)
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


def _bind_session(chat_id: str, sid: str, cwd) -> list:
    """把会话绑定到聊天。工作目录跟随会话所在项目，但只固化进本聊天的绑定，
    不动全局 WORK_DIR——否则 A 聊天接管会破坏 B 聊天的会话续接。

    会话独占：同一会话同一时间只归一个聊天。绑定时若已被其他聊天占用，
    所有权转移给本聊天（后接管者优先，否则「实际工作的永远是第一个聊天」）：
    原聊天解绑、驻留进程让位并收到通知，避免两个聊天各起进程把同一会话
    写成两条时间线。返回被挤掉的聊天列表（空=无冲突）。

    注意：会调用 _recycle_chat_sessions（内部拿 _running_lock），不能在
    持有 _running_lock 时调用本函数。
    """
    if cwd and Path(cwd).is_dir():
        wd = str(Path(cwd).resolve())
    else:
        wd = str(WORK_DIR)
    evicted = []
    with _sessions_lock:
        for other, info in list(_sessions.items()):
            if other != chat_id and (info or {}).get("session_id") == sid:
                del _sessions[other]
                evicted.append(other)
        _sessions[chat_id] = {
            "session_id": sid,
            "work_dir": wd,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
    save_sessions()
    # 本聊天若在观察这个会话：会话归你了，实时进度走流式卡片，观察自动关闭
    _stop_observer(chat_id, f"📡 已接管会话 {sid[:8]}…，实时进度走流式卡片，观察自动关闭。",
                   sid=sid)
    for other in evicted:
        _recycle_chat_sessions(other, reason="takeover")
        send_text(other, f"↪️ 会话 {sid[:8]}… 已被另一个聊天接管，本聊天已解绑"
                         f"（历史保留，/takeover {sid[:8]} 可重新接管）。")
    return evicted


# ---------------- 终端移交标记（takeover 接管落地 → 终端 Stop hook 消费） ----------------
HANDOFF_FILE = BASE_DIR / "handoff_requests.json"   # sid -> {"reason", "ts"}
TERMINAL_HANDOFF_PROMPT = (
    "【飞书已接管本会话】你的工作已由飞书端的分身接手，请立即收尾、不要开始新的工作："
    "①简要说明当前进行到哪一步、还有什么没完成；"
    "②清点你拉起的后台任务（如 run_in_background 的监控脚本）：逐个说明任务内容、PID、"
    "输出位置、最近一次结果，并给出原样的启动命令（脚本路径+参数）——不要停掉它们，"
    "接管方将按此清单继续跟进监控；若它们随本会话退出而中断，接管方按启动命令重启即可；"
    "③说完即停。这段说明会留在转录里，由接管方读取。")


def _mark_terminal_handoff(sid: str) -> None:
    """takeover 接管落地时给该会话的终端宿主留移交指令。

    终端进程没有可注入消息的通道（stdin 是用户终端的），唯一抓手是它的
    Stop hook（notify.py）：回合结束触发时查到标记 → block 一轮把指令喂给
    终端 Claude，跑完手头回合就被"劝停交出"。空闲的终端不触发 Stop，标记
    留到它下次活动时生效；分身（FEISHU_BOT_CHILD=1）的 Stop 不查标记。
    """
    if not sid:
        return
    try:
        data = json.loads(HANDOFF_FILE.read_text(encoding="utf-8")) \
            if HANDOFF_FILE.exists() else {}
    except Exception:
        data = {}
    data[sid] = {"reason": TERMINAL_HANDOFF_PROMPT, "ts": time.time()}
    try:
        tmp = HANDOFF_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HANDOFF_FILE)
        log(f"[移交标记] 已给会话 {sid[:8]}… 的终端宿主留移交指令")
    except Exception as e:
        log(f"[移交标记] 写入失败：{e!r}")


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
        _mark_terminal_handoff(pend["sid"])   # 终端跑完这轮 Stop 时会被劝停交出
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
    old_sid = (_sessions.get(chat_id) or {}).get("session_id")
    if old_sid and old_sid != sid:
        _remember_last(chat_id, old_sid)   # 换绑前的旧会话，/takeover last 可接回
    if val == "force":
        _recycle_chat_sessions(chat_id)   # 换绑：旧会话的驻留进程让位
        evicted = _bind_session(chat_id, sid, cwd)
        _mark_terminal_handoff(sid)   # 终端跑完手头回合 Stop 时会被劝停交出
        stolen = "\n📌 该会话此前绑定在另一个聊天，已转移过来（对方已收到通知）。" if evicted else ""
        send_text(chat_id,
                  f"✅ 已强制接管会话 {sid[:8]}…（目录：{_chat_work_dir(chat_id)}）\n"
                  "下一条消息将带上该会话的全部上下文。\n"
                  "· 已给终端留移交指令：它跑完手头回合会自动收尾交出"
                  "（含后台任务台账），之后不再接活。" + stolen)
        return
    # queue：绑定 + 排队接管（沿用 _wait_and_drain 轮询机制）
    _recycle_chat_sessions(chat_id)   # 换绑：旧会话的驻留进程让位
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


def _active_child_count(proc: subprocess.Popen) -> int:
    """数 claude 驻留进程名下还在跑的工具子进程（后台编译/巡检之类）。

    排除常驻"脊柱"：claude.cmd 垫片 → node(cli.js) → perm_mcp.py——它们随主进程
    生灭，不算活跃工作。psutil 不可用/查询失败返回 0（退回纯时间判定）。"""
    if psutil is None:
        return 0
    try:
        kids = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        return 0
    active = 0
    for k in kids:
        try:
            cl = " ".join(k.cmdline()).lower()
        except Exception:
            cl = ""
        if "cli.js" in cl or "perm_mcp" in cl or "claude.cmd" in cl:
            continue
        active += 1
    return active


# ---------------- 运行日志（打印 + 落盘 bot.log，机器人窗口看不到时远程排障用） ----------------
LOG_FILE = BASE_DIR / "bot.log"


def log(msg: str) -> None:
    """关键路径日志：控制台打印之外追加到 bot.log（带时间戳，UTF-8）。"""
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- 发送消息 ----------------
_send_lock = threading.Lock()
_last_send = 0.0


def _split_chunks(text: str, cap: int = MAX_CHUNK) -> list:
    """长文本分段：按行贪心装段；表格块（连续含 | 行）作为原子整体不拆——
    拆开的半张表在卡片里都渲染不出来。内容不丢；段间空行当分隔吃掉属正常折损。
    """
    lines = (text or "").split("\n")
    out: list = []
    cur: list = []
    cur_len = 0
    i, n = 0, len(lines)
    while i < n:
        # 连续含 | 的行归并为一个原子块（宽松判定：误判只是切割点偏移，无害）
        if "|" in lines[i]:
            j = i
            while j < n and "|" in lines[j]:
                j += 1
            block = lines[i:j]
        else:
            j = i + 1
            block = [lines[i]]
        blen = sum(len(x) + 1 for x in block)
        if cur and cur_len + blen > cap:   # 装不下先封当前段
            out.append("\n".join(cur).rstrip("\n"))
            cur, cur_len = [], 0
        if blen > cap and len(block) > 1:
            # 超长表格：认命按行装（极罕见，不丢内容优先）
            for ln in block:
                if cur and cur_len + len(ln) + 1 > cap:
                    out.append("\n".join(cur).rstrip("\n"))
                    cur, cur_len = [], 0
                cur.append(ln)
                cur_len += len(ln) + 1
        elif blen > cap:
            seg = block[0]                  # 单行超限：硬切
            while len(seg) > cap:
                out.append(seg[:cap])
                seg = seg[cap:]
            cur.append(seg)
            cur_len += len(seg) + 1
        else:
            cur.extend(block)
            cur_len += blen
        i = j
    if cur:
        out.append("\n".join(cur).rstrip("\n"))
    return out


def _is_table_sep(line: str) -> bool:
    """GFM 表格的分隔行（|---|:---:| 形态）。"""
    s = set((line or "").strip())
    return "|" in s and s <= {"|", "-", ":", " "}


def _md_tables_need_blank(text: str) -> str:
    """表格前必须空行，紧贴段落会导致整表不渲染（Claude 输出常见形态）。

    分隔行的上一行是表头行；表头行若紧贴非空段落（out[-2] 非空），
    在表头行**之前**补空行（插到表头和分隔行之间会把表格拆散）。
    幂等：已补过（表头上是空行）不重复补。
    """
    lines = (text or "").split("\n")
    out: list = []
    for i, ln in enumerate(lines):
        if (i >= 2 and _is_table_sep(ln)
                and out[-1].strip() and out[-2].strip()):
            out.insert(len(out) - 1, "")   # 表头前补空行
        out.append(ln)
    return "\n".join(out)


def send_text(chat_id: str, text: str) -> None:
    """发送文本到指定会话：超长自动分段（换行处断开，不丢内容），带简单限速。"""
    global _last_send
    text = (text or "").strip()
    if not text:
        return
    for chunk in _split_chunks(text):
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
                    log(f"[发消息失败] code={resp.code} msg={resp.msg}")
            except Exception as e:
                log(f"[发消息异常] {e!r}")


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
            log(f"[发卡片失败] code={resp.code} msg={resp.msg}")
            return None
        return resp.data.message_id if resp.data else None
    except Exception as e:
        log(f"[发卡片异常] {e!r}")
        return None


def _info_card(title: str, md: str, template: str = "blue") -> dict:
    """信息类卡片（/help /status 等）的统一外壳。

    卡片 2.0 + markdown 组件：1.0 JSON 的 lark_md/markdown 元素实测都不
    渲染（列表/标题/代码块原样显示），2.0 才是完整语法渲染（已实验验证）。
    """
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": template},
            "body": {"elements": [{"tag": "markdown", "content": _md_tables_need_blank(md)}]}}


class _LiveReply:
    """Claude 流式回复的实时卡片：文本增量节流刷新，块末定格；超长自动续新卡片。

    卡片发送/更新失败自动降级为普通文本消息（整块补发），内容不丢。
    由会话读泵线程独占使用（同一聊天的输出天然串行，无需加锁）。
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.msg_id = None      # 当前卡片消息 id
        self.buf = ""           # 本文本块已积累的内容
        self.sealed = None      # 已定格进卡片的前段（buf 超过单卡上限时）
        self.ok = True          # 卡片通道可用（失败后停止流式，块末走文本降级）
        self._last_patch = 0.0

    def _card(self, content: str, streaming: bool) -> dict:
        # 卡片 2.0 + markdown 组件：Claude 回复本身就是 markdown，1.0 的
        # lark_md/markdown 元素实测不渲染（原样显示），2.0 才是完整语法。
        # 生成中提示并入内容尾部（2.0 少挂组件，零结构风险）。
        c = _md_tables_need_blank(content or "…")
        if streaming:
            c += "\n\n*(✍️ 正在生成…)*"
        return {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": c}]}}

    def _patch(self, content: str, streaming: bool) -> None:
        """把当前卡片整卡更新为指定内容；失败置 ok=False（后续走文本降级）。"""
        try:
            req = PatchMessageRequest.builder() \
                .message_id(self.msg_id) \
                .request_body(PatchMessageRequestBody.builder()
                              .content(json.dumps(self._card(content, streaming),
                                                  ensure_ascii=False))
                              .build()) \
                .build()
            resp = api.im.v1.message.patch(req)
            if not resp.success():
                log(f"[流式卡片更新失败] code={resp.code} msg={resp.msg}")
                self.ok = False
        except Exception as e:
            log(f"[流式卡片更新异常] {e!r}")
            self.ok = False
        self._last_patch = time.time()

    def feed(self, chunk: str) -> None:
        """流进一个文本增量：首帧建卡，之后节流整卡刷新；超限先定格满段。"""
        self.buf += chunk
        if self.msg_id is None:
            # 首帧就超限时只放前段，避免卡片内容超限被整卡拒绝
            preview = self.buf if len(self.buf) < MAX_CHUNK else _split_chunks(self.buf)[0]
            self.msg_id = _send_card(self.chat_id, self._card(preview, True))
            self._last_patch = time.time()
            if self.msg_id is None:
                self.ok = False
                return
        if not self.ok:
            return
        if self.sealed is not None:
            return   # 满段已定格：尾部攒到块末 close 时一起补发
        if len(self.buf) >= MAX_CHUNK:
            self.sealed = _split_chunks(self.buf)[0]
            self._patch(self.sealed, streaming=False)
            return
        if time.time() - self._last_patch >= LIVE_PATCH_INTERVAL:
            self._patch(self.buf, streaming=True)

    def close(self, final=None) -> bool:
        """文本块结束：整块定格（去"生成中"角标；剩余内容续新卡片）。

        返回 False = 卡片没能承载（调用方用普通文本补发整块，内容不丢）。
        """
        text = self.buf if final is None else final
        if self.msg_id is None or not self.ok:
            return False
        if self.sealed is not None:
            if not text.startswith(self.sealed):
                return False   # 极端情况（增量与成块文本不一致）：整块走文本降级
            rest = text[len(self.sealed):]
        else:
            chunks = _split_chunks(text)
            self._patch(chunks[0], streaming=False)
            if not self.ok:
                return False
            rest = "".join(chunks[1:])
        if rest.strip():
            for c in _split_chunks(rest):
                mid = _send_card(self.chat_id, self._card(c, False))
                if mid is None:
                    send_text(self.chat_id, c)   # 续卡失败：该段走文本，不丢内容
                else:
                    self.msg_id = mid
        return True


# ---------------- 待决策卡片（权限确认 / 接管选择等，支持按钮+文字自定义回复） ----------------
_choices_lock = threading.Lock()
_choices: dict = {}   # chat_id -> {"id","event","answer","tag","labels"}


def _freeze_choice_card(msg_id, card: dict, answer) -> None:
    """用户已答复（点按钮/回文字/超时/取消）：把卡片更新成定格状态。

    在选项上直接体现：选中项变主色并标（已选择），其余按钮置灰不可点；
    文字/超时/取消则在卡片尾部附一行说明。patch 失败重试兜底（共 3 次，
    间隔 1 秒，覆盖瞬态失败/频控），全部失败只打日志不影响流程。
    """
    if not msg_id:
        return
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
    for attempt in range(3):
        try:
            resp = api.im.v1.message.patch(req)
            if resp.success():
                return
            log(f"[卡片定格失败] 第{attempt + 1}/3次 msg_id={msg_id} "
                f"code={resp.code} msg={resp.msg}")
        except Exception as e:
            log(f"[卡片定格异常] 第{attempt + 1}/3次 msg_id={msg_id} {e!r}")
        if attempt < 2:
            time.sleep(1)


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
    # timeout<=0 或 None = 无限等待（权限确认与桌面终端一致，不会自动放弃；
    # claude 进程退出时由 _cancel_perm_choice 兜底返回 cancel）
    entry["event"].wait(timeout if timeout and timeout > 0 else None)
    with _choices_lock:
        if _choices.get(chat_id) is entry:
            _choices.pop(chat_id, None)
    answer = entry["answer"] if entry["answer"] is not None else ("timeout", None)
    log(f"[决策] ask={req_id[-8:]} kind={answer[0]} val={str(answer[1])[:40]} "
        f"→ 定格卡片 msg_id={msg_id}")
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


# ---------------- 桌面权限等待标记（perm_gate.py 转来的确认，/peek 与 /observe 显示） ----------------
PENDING_PERM_FILE = BASE_DIR / "pending_perm.json"
_pending_perm_lock = threading.Lock()
_pending_perm: dict = {}    # sid -> {"ts": float, "tool": str}
if PENDING_PERM_FILE.exists():
    try:
        _pending_perm = json.loads(PENDING_PERM_FILE.read_text("utf-8"))
    except Exception:
        _pending_perm = {}


def _save_pending_perm() -> None:
    with _pending_perm_lock:
        data = dict(_pending_perm)
    try:
        PENDING_PERM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def _note_pending_perm(sid: str, tool: str) -> None:
    if not sid:
        return
    with _pending_perm_lock:
        _pending_perm[sid] = {"ts": time.time(), "tool": tool}
    _save_pending_perm()


def _clear_pending_perm(sid: str) -> None:
    if not sid:
        return
    with _pending_perm_lock:
        existed = _pending_perm.pop(sid, None) is not None
    if existed:
        _save_pending_perm()


def _pending_perm_line(sid: str, sf: Path) -> str:
    """该会话是否卡在桌面权限确认（perm_gate 发起了转发且转录尚未恢复写入）。
    转录恢复写入（权限已解决、工具开始跑）或超过 30 分钟即自动清理。
    返回展示行；无等待/已过期返回空串。"""
    with _pending_perm_lock:
        pp = _pending_perm.get(sid)
    if not pp:
        return ""
    if time.time() - pp.get("ts", 0) > 1800:
        _clear_pending_perm(sid)
        return ""
    try:
        if sf.stat().st_mtime > pp.get("ts", 0) + 2:   # 标记后转录又动过 = 卡已解除
            _clear_pending_perm(sid)
            return ""
    except OSError:
        pass
    mins = int(time.time() - pp.get("ts", 0)) // 60
    wait = f"已等 {mins} 分钟" if mins >= 1 else "刚刚"
    return f"⏸ 桌面端在等你确认权限（{pp.get('tool', '?')}，{wait}）——确认卡已推到飞书，点 ✅ 放行 / ❌ 拒绝"


DESKTOP_ROUTE_FILE = BASE_DIR / "desktop_route.json"   # sid -> chat_id（供 notify.py 查）


def _desktop_route(sid: str) -> str:
    """按会话反查关联聊天：当前绑定优先，其次让位记录（last）。命中返回 chat_id。

    桌面事件（权限卡/通知）自带 session_id，路由按"哪个聊天接管过/让出过
    这个会话"走，而不是"用户最后在哪说话"。
    """
    if not sid:
        return ""
    with _sessions_lock:
        for chat, info in _sessions.items():
            if (info or {}).get("session_id") == sid:
                return chat
    with _last_lock:
        for chat, item in _last_sessions.items():
            if (item or {}).get("sid") == sid:
                return chat
    return ""


def _save_desktop_route() -> None:
    """把会话→聊天路由落盘，notify.py（桌面通知 hook，独立进程）据此推送。

    来源：当前绑定（_sessions，优先）+ 让位记录（_last_sessions 补缺）。
    幂等全量重建，绑定关系每次变化后调用。
    """
    route = {}
    with _sessions_lock:
        for chat, info in _sessions.items():
            sid = (info or {}).get("session_id")
            if sid:
                route[sid] = chat
    with _last_lock:
        for chat, item in _last_sessions.items():
            sid = (item or {}).get("sid")
            if sid and sid not in route:
                route[sid] = chat
    try:
        DESKTOP_ROUTE_FILE.write_text(json.dumps(route, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _desktop_perm_target(sid: str = "") -> str:
    """桌面事件的推送目标聊天：优先按会话路由（接管过/让出过它的聊天），
    未命中退回 notify_target.json（最后活跃聊天，与 notify.py 兜底一致）。"""
    chat = _desktop_route(sid)
    if chat:
        return chat
    try:
        return json.loads((BASE_DIR / "notify_target.json").read_text("utf-8")).get("chat_id") or ""
    except Exception:
        return ""


def _perm_server() -> None:
    """本地 TCP 通道：perm_mcp.py（claude 的权限回调 MCP 服务器）把请求转过来。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", PERM_PORT))
    except OSError as e:
        log(f"[权限通道] 端口 {PERM_PORT} 绑定失败：{e!r}（权限确认将自动拒绝，其余功能不受影响）")
        return
    srv.listen(8)
    log(f"[权限通道] 127.0.0.1:{PERM_PORT} 已监听")
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
        elif payload and payload.get("type") == "desktop_perm":
            # 桌面终端会话的权限确认（perm_gate.py hook 转来）：按 sid 路由到
            # 接管过/让出过该会话的聊天，未命中退回最后活跃聊天
            sid = str(payload.get("sid") or "")
            chat_id = _desktop_perm_target(sid)
            tool = str(payload.get("tool_name") or "?")
            if not chat_id:
                result = None   # bot 还没收过消息（无通知目标）：不答复，hook 让路终端
            else:
                disp = _perm_input_display(tool, payload.get("tool_input"))
                _note_pending_perm(sid, tool)
                kind, val = _ask_choice(
                    chat_id, "🖥️ 桌面会话请求权限",
                    f"**工具**：`{tool}`\n**内容**：\n{disp}",
                    [{"label": "✅ 允许", "value": "allow", "style": "primary"},
                     {"label": "❌ 拒绝", "value": "deny", "style": "danger"}],
                    timeout=PERM_GATE_WAIT, template="orange", tag="desktop_perm",
                    note="桌面终端上跑的会话在等这个确认。也可以回复文字：`允许` / `拒绝` /"
                         " 任意说明（说明会作为拒绝原因转给桌面 Claude）。\n"
                         "超时未应答则回退到桌面终端弹框（人回电脑上点）。")
                _clear_pending_perm(sid)
                if kind == "choice":
                    result = ({"behavior": "allow"} if val == "allow" else
                              {"behavior": "deny", "message": "用户在飞书上拒绝了本次操作"})
                elif kind == "text":
                    t = (val or "").strip().lower()
                    if t in PERM_ALLOW_WORDS:
                        result = {"behavior": "allow"}
                    elif t in PERM_DENY_WORDS:
                        result = {"behavior": "deny", "message": "用户在飞书上拒绝了本次操作"}
                    else:
                        result = {"behavior": "deny", "message": f"用户回复：{val}"}
                else:
                    result = None   # 超时/被顶替：不答复，桌面终端弹框接手
                    send_text(chat_id, f"⏳ 桌面权限确认（{tool}）超时未应答，已回退桌面终端弹框。")
    except Exception as e:
        log(f"[权限确认处理出错] {e!r}")
        result = {"behavior": "deny", "message": "权限确认处理内部错误"}
    finally:
        if result is not None:   # None = 不答复（超时/无目标）：hook 读到 EOF 让路终端
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


# ---------------- 长驻会话进程（stream-json 双向，2026-10-27 实测工具间隙可注入） ----------------
SESSION_IDLE_TIMEOUT = int(os.environ.get("SESSION_IDLE_TIMEOUT", "900"))  # 闲置回收（秒）


def _user_msg_line(text: str) -> str:
    """stream-json 输入格式的一行用户消息。"""
    return json.dumps({"type": "user", "message": {"role": "user",
                                                   "content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def _write_to_session(slot: dict, text: str) -> bool:
    """把用户消息写进长驻进程的 stdin（运行中会在工具间隙被读取；空闲则开启下一回合）。

    写入需按 slot 锁串行化（可能多线程同时递消息）；管道断裂返回 False，由上层重建。
    """
    proc = slot.get("proc")
    if proc is None or proc.poll() is not None:
        return False
    try:
        with slot["wlock"]:
            proc.stdin.write(_user_msg_line(text))
            proc.stdin.flush()
        slot["last_event"] = time.time()
        slot["turn_start"] = time.time()
        slot["in_turn"] = True
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def _recycle_chat_sessions(chat_id: str, reason: str = "recycle") -> None:
    """终结该聊天名下的所有长驻会话进程（/new /cwd /takeover 换绑时用）。"""
    with _running_lock:
        last_sid = None
        for key, slot in list(_running.items()):
            if slot.get("chat_id") == chat_id and slot.get("proc") is not None:
                last_sid = slot.get("sid") or last_sid
                slot["reason"] = reason
                kill_task(slot["proc"])
    if last_sid:   # 记下让位的会话，/takeover last 一键接回（/exit /handover/换绑都走这里）
        _remember_last(chat_id, last_sid)


# ---------------- 让位会话记忆 + 移交收尾（/takeover last · /handover） ----------------
LAST_FILE = BASE_DIR / "last.json"      # chat_id -> {sid, work_dir, ts}
_last_lock = threading.Lock()
_last_sessions: dict = {}
if LAST_FILE.exists():
    try:
        _last_sessions = json.loads(LAST_FILE.read_text(encoding="utf-8"))
    except Exception:
        _last_sessions = {}

# /handover 等待收尾的聊天：chat_id -> {sid, cmd_line, ts}（收尾回合结束或超时由 _finalize 消费）
_handover_lock = threading.Lock()
_handovers: dict = {}
HANDOVER_WAIT = 180   # 分身收尾总结的最长等待（秒），超时强制移交


def _remember_last(chat_id: str, sid: str) -> None:
    """聊天让位某会话时记一笔，/takeover last 据此一键接回。"""
    with _last_lock:
        _last_sessions[chat_id] = {"sid": sid,
                                   "work_dir": (_sessions.get(chat_id) or {}).get("work_dir"),
                                   "ts": time.time()}
        try:
            LAST_FILE.write_text(json.dumps(_last_sessions, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    _save_desktop_route()   # 让位关系变化，路由文件随最新鲜


def _finalize_handover(chat_id: str, forced: bool) -> None:
    """/handover 的落地动作：分身收尾完成后（或超时强推）真正移交终端。"""
    with _handover_lock:
        info = _handovers.pop(chat_id, None)
    if not info:
        return   # 已被另一路收尾（result 先到/超时先到），幂等退出
    sid = info["sid"]
    label = _names.get(sid) or f"{sid[:8]}…"
    cmd_line = info["cmd_line"]
    # 走 PowerShell Set-Clipboard：CreateProcess 参数原生 UTF-16，中文不经码页转换
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Set-Clipboard -Value '" + cmd_line.replace("'", "’") + "'"],
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        clip_note = "✅ 接手命令已复制到剪贴板——到任意终端 Ctrl+V 回车即续跑"
    except Exception:
        clip_note = "⚠️ 剪贴板复制失败，手动执行：\n" + cmd_line
    _remember_last(chat_id, sid)   # /takeover last 可一键接回
    _recycle_chat_sessions(chat_id, reason="exit")   # 分身退场
    with _pending_lock:
        _pending.pop(chat_id, None)
    with _sessions_lock:
        _sessions.pop(chat_id, None)
    save_sessions()
    done_note = ("· 收尾总结已写入转录，终端接手第一眼可见\n" if not forced else
                 "⚠️ 收尾超时未完成已强制移交——半程进度都在转录里，终端可续\n")
    send_text(chat_id, f"🤝 会话已移交终端：{label}\n"
                       f"{done_note}"
                       f"· {clip_note}\n"
                       f"· 若终端还开着本会话的进程，先 Ctrl+C 关掉再粘贴（避免两边同时写入）\n"
                       "· /takeover last 随时把它接回飞书")


def run_task(chat_id: str, prompt: str) -> None:
    """任务入口：每个会话一个长驻 claude 进程（stream-json 双向）。

    进程活着 → 消息直接写 stdin：正在执行的任务会在工具调用间隙读到它、
    边跑边回应（和桌面终端一致的体验）；进程不在（首次/闲置回收/崩溃）→
    新起一个，绑定会话自动 --resume 续接。互斥键是会话（_task_key）：
    同一会话被另一聊天占用时拒绝，不同会话互不阻塞。
    """
    with _running_lock:
        # key 在锁内算（init 绑定与键迁移都在锁内完成，避免拿到半程状态重复起进程）
        key = _task_key(chat_id)
        slot = _running.get(key)
        if slot is None and key != chat_id:
            # 兜底：init 刚绑定会话、slot 还没从 chat_id 键迁移到会话键的窗口期
            cand = _running.get(chat_id)
            if cand is not None and cand.get("chat_id") == chat_id and cand.get("sid") == key:
                slot = cand
        if slot is not None:
            if slot.get("chat_id") != chat_id:
                send_text(chat_id, f"⚠️ 会话 {key[:8]}… 正在被另一个聊天使用，"
                                   f"已拒绝（避免两边各写一条时间线）。"
                                   f"用 /takeover {key[:8]} 可把会话接管过来。")
                return
            if _write_to_session(slot, prompt):
                send_text(chat_id, "💬 已送达当前会话（正在执行的话会在工具间隙读取，"
                                   "Claude 会根据进度决定何时回应）")
                return
        # 没有活进程：在锁内完成 起进程+首条消息写入，保证消息次序不乱
        slot = {"chat_id": chat_id, "proc": None, "started": time.time(), "reason": None,
                "wlock": threading.Lock(), "last_event": time.time(),
                "turn_start": time.time(), "in_turn": False, "sid": _lookup_session(chat_id),
                "mode": _chat_mode(chat_id),   # 起进程时用的权限模式（/mode 切换后检测重建用）
                "provider": _provider_now()}   # 起进程时的供应商（/status 对比换没换 key）
        try:
            proc = _spawn_claude(chat_id, slot["sid"])
        except FileNotFoundError:
            send_text(chat_id, "❌ 找不到 claude 命令，请在 .env 中用 CLAUDE_PATH 指定完整路径。")
            return
        except OSError as e:
            send_text(chat_id, f"❌ 启动 claude 进程失败：{e!r}")
            return
        slot["proc"] = proc
        _running[key] = slot
        _write_to_session(slot, prompt)
    label = _chat_label(chat_id)
    tag = f"续接 {label} · {_mode_label(chat_id)}" if label else f"新会话 · {_mode_label(chat_id)}"
    send_text(chat_id, f"🤖 收到（{tag}），开始执行…")
    threading.Thread(target=_session_reader, args=(chat_id, key, slot), daemon=True).start()
    threading.Thread(target=_session_watchdog, args=(chat_id, key, slot), daemon=True).start()


def _spawn_claude(chat_id: str, session_id):
    """起长驻 claude 进程（stream-json 输入/输出；stdin 保持打开持续接收消息）。

    注意：提示词通过 stdin 传入，避免经 .cmd 垫片时被 cmd.exe 解析特殊字符。
    """
    cmd = [CLAUDE_PATH, "-p", "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    mode = _chat_mode(chat_id)
    if mode and mode != "default":   # CLI 的 --permission-mode 不收 "default"，省略即默认
        cmd += ["--permission-mode", mode]
    # 权限确认走飞书卡片：claude 遇到权限提示时调用 MCP 工具
    # （perm_mcp.py → 本地 socket → 卡片/文字回复）。bypassPermissions 不会产生提示，跳过。
    if mode != "bypassPermissions":
        mcp_config = {"mcpServers": {"feishu": {
            "command": sys.executable, "args": [str(BASE_DIR / "perm_mcp.py")]}}}
        cmd += ["--permission-prompt-tool", "mcp__feishu__request_permission",
                "--mcp-config", json.dumps(mcp_config, separators=(",", ":"))]
    if CLAUDE_EXTRA_ARGS:
        cmd += shlex.split(CLAUDE_EXTRA_ARGS)
    # 聊天级模型（/model 设置）：显式指定优先级最高，压过 settings.json 与 env 别名
    _m = _chat_models.get(chat_id)
    if _m:
        cmd += ["--model", _m]
    if session_id:
        cmd += ["--resume", session_id]

    # FEISHU_BOT_CHILD=1：让 notify.py 跳过机器人自己的 claude 子进程，避免重复通知
    # FEISHU_CHAT_ID/FEISHU_PERM_PORT：perm_mcp.py 借此路由卡片和连回本进程
    # CLAUDE_CODE_GIT_BASH_PATH：强制分身用 Bash 工具（Git Bash），白名单里的
    #   Bash(...) 规则才能命中——否则 Windows 非交互 spawn 默认 PowerShell 工具
    child_env = {**os.environ, "FEISHU_BOT_CHILD": "1",
                 "FEISHU_CHAT_ID": chat_id, "FEISHU_PERM_PORT": str(PERM_PORT)}
    if GIT_BASH:
        child_env["CLAUDE_CODE_GIT_BASH_PATH"] = GIT_BASH
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        cwd=str(_chat_work_dir(chat_id)), env=child_env,
    )


def _session_reader(chat_id: str, key: str, slot: dict) -> None:
    """长驻进程输出泵：转发回复/工具调用，逐回合汇报；进程退出时收尾并清理。"""
    proc = slot["proc"]
    stderr_buf: list = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_buf.append(line)

    threading.Thread(target=_drain_stderr, daemon=True).start()

    last_sent = ""   # 本回合已实际转发过的最后一段 assistant 文本（result 与之去重；静默模式下保持为空）
    live = None      # 当前流式回复卡片（stream_event 增量驱动，assistant 成块事件收尾）
    try:
        for line in proc.stdout:
            line = line.strip()
            slot["last_event"] = time.time()   # 任何事件都算活跃（含 thinking_tokens / stream_event）
            if not line.startswith("{"):
                continue  # 忽略夹杂的非 JSON 行
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "system" and evt.get("subtype") == "init":
                sid = evt.get("session_id")
                if sid:
                    slot["sid"] = sid
                    # 绑定会话：下一条消息（若进程已回收）自动 --resume 续接
                    _bind_session(chat_id, sid, str(_chat_work_dir(chat_id)))
                    # 首次会话的 slot 原先用 chat_id 作键：迁移到会话键，
                    # 后续 _task_key(chat_id) 才能命中（否则会重复起进程）
                    with _running_lock:
                        for k, s in list(_running.items()):
                            if s is slot and k != sid:
                                del _running[k]
                        _running.setdefault(sid, slot)
            elif etype == "stream_event":
                # 流式增量（--include-partial-messages）：只关心回复文本，
                # thinking_delta / 工具入参增量不展示（静默诉求：思考过程不打扰）
                sev = evt.get("event") or {}
                if sev.get("type") == "content_block_delta":
                    d = sev.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text") \
                            and chat_id not in _quiet:
                        if live is None:
                            live = _LiveReply(chat_id)
                        live.feed(d["text"])
            elif etype == "assistant":
                for block in (evt.get("message") or {}).get("content") or []:
                    btype = block.get("type")
                    if btype == "text" and (block.get("text") or "").strip():
                        final = block["text"].strip()
                        if live is not None:
                            # 流式卡片收尾定格（超长续卡 / 通道失败降级都在里面）
                            if not live.close(final):
                                send_text(chat_id, final)
                            live = None
                            last_sent = final
                        elif chat_id not in _quiet:
                            # 没走流式（卡片通道降级/静默刚开启）：普通文本兜底
                            send_text(chat_id, final)
                            last_sent = final
                    elif btype == "tool_use" and FORWARD_TOOLS and chat_id not in _quiet:
                        if live is not None:   # 文本后紧跟工具调用：先定格卡片再发 🔧
                            live.close()
                            live = None
                        send_text(chat_id, "🔧 " + _tool_summary(block.get("name", "?"), block.get("input")))
            elif etype == "result":
                if live is not None:   # 异常路径兜底：定格尚未收尾的卡片
                    live.close()
                    live = None
                slot["in_turn"] = False
                _finish_turn(chat_id, evt, last_sent, slot)
                last_sent = ""
    except Exception as e:
        log(f"[会话读泵异常] {e!r}")
    returncode = proc.wait()
    if live is not None:   # 进程退出仍有未收尾卡片：用已积累内容定格，不丢
        live.close()
        live = None
    _cancel_perm_choice(chat_id)   # claude 已退出（跑完/被停），挂着的权限确认不再等
    tail = "".join(stderr_buf[-12:]).strip()
    reason = slot.get("reason")
    with _running_lock:
        # slot 可能登记在会话键下（init 后迁移过），两个键都查
        for k in (key, slot.get("sid")):
            if k and _running.get(k) is slot:
                _running.pop(k, None)
    if reason == "stop":
        send_text(chat_id, "⛔ 已手动中断。")
    elif reason == "timeout":
        send_text(chat_id, f"⚠️ 执行超过 {CLAUDE_TIMEOUT}s，已强制中断。可在 .env 中调大 CLAUDE_TIMEOUT。")
    elif reason == "idle":
        send_text(chat_id, "💤 会话进程已闲置回收（上下文已保存），随时发消息继续。")
    elif reason == "recycle":
        pass  # 主动让位（/new /cwd /takeover 换绑），命令本身已有回复
    elif reason == "handover":
        pass  # /handover 中断让位：正在 --resume 重生跑收尾总结，不打扰
    elif reason == "exit":
        pass  # 主动退出/移交（/exit、/handover 落地）taskkill 强杀退出码非 0，
              # 不算异常——命令本身已有回执，别再发"进程异常退出"
    elif returncode != 0:
        if slot.get("sid") and ("session" in tail.lower() or "conversation" in tail.lower()):
            # --resume 失效（目录切换/记录损坏等）
            with _sessions_lock:
                _sessions.pop(chat_id, None)
            save_sessions()
            send_text(chat_id, "❌ 原会话已失效（绑定已清除），再发一次消息将自动开新会话。")
        else:
            low = tail.lower()
            hint = ("\n⚠️ 疑似供应商额度耗尽/限流——/provider 查看并切换。"
                    if any(k in low for k in ("429", "rate limit", "quota",
                                               "insufficient", "balance")) else "")
            send_text(chat_id, f"❌ Claude 进程异常退出（code={returncode}）\n"
                               f"{tail[-800:] or '（无错误输出）'}{hint}")
    # 会话暂告一段落（闲置回收/主动退出）：补一份摘要供 /sessions 列表用。
    # 不含 recycle（换绑/模式重建时会话还在继续）——等真正闲置了再总结。
    if reason in ("idle", "exit") and slot.get("sid"):
        _maybe_summarize(slot.get("sid"))


def _finish_turn(chat_id: str, result: dict, last_text: str, slot: dict) -> None:
    """单个回合结束的汇报（长驻进程里每条消息各有一个 result 事件）。"""
    final_text = (result.get("result") or "").strip()
    if final_text and final_text != last_text:
        send_text(chat_id, final_text)

    dur = (result.get("duration_ms") or 0) / 1000
    cost = result.get("total_cost_usd") or 0
    turns = result.get("num_turns", "?")
    label = _chat_label(chat_id)
    line = f"✅ {label + ' ' if label else ''}完成（{_mode_label(chat_id)}） · 耗时 {dur:.1f}s · {turns} 轮对话"
    if cost:
        line += f" · ${cost:.3f}"
    if result.get("is_error"):
        line = "⚠️ 任务异常结束 · " + line
        low = (final_text or "").lower()
        if any(k in low for k in ("429", "rate limit", "quota",
                                  "insufficient", "balance", "credit")):
            line += "\n⚠️ 疑似供应商额度耗尽/限流——/provider 查看并切换。"
    send_text(chat_id, line)

    denials = result.get("permission_denials") or []
    if denials:
        send_text(chat_id, f"⚠️ 有 {len(denials)} 个操作因权限被拒绝。"
                           "如需放开：发 /mode bypassPermissions（机器人级设置，注意风险），"
                           "或把具体命令加进 Claude 设置的 allow 列表。")

    # /mode 按聊天独立设置，而驻留进程的模式在起进程时就固定了：本聊天的模式变过的话
    # 本回合结束后优雅退役（关 stdin），下一条消息用新模式重建
    if slot.get("mode") and slot.get("mode") != _chat_mode(chat_id):
        try:
            slot["reason"] = "recycle"
            if slot.get("proc") and slot["proc"].poll() is None:
                slot["proc"].stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    # /handover 等收尾的聊天：收尾总结回合一结束就落地移交（进程稍后由 _finalize 杀掉）
    if _handovers:
        _finalize_handover(chat_id, forced=False)


def _session_watchdog(chat_id: str, key: str, slot: dict) -> None:
    """长驻进程看门狗：闲置太久优雅回收（关 stdin）；CLAUDE_TIMEOUT>0 时单回合超时强杀。"""
    while True:
        time.sleep(15)
        proc = slot.get("proc")
        if proc is None or proc.poll() is not None:
            return  # 进程已退出，读泵负责收尾
        with _running_lock:
            if not any(_running.get(k) is slot for k in (key, slot.get("sid"))):
                return  # 该会话已换绑/重建
        now = time.time()
        if CLAUDE_TIMEOUT > 0 and slot.get("in_turn") \
                and now - slot.get("turn_start", now) > CLAUDE_TIMEOUT:
            slot["reason"] = "timeout"
            kill_task(proc)
            return
        # 闲置回收只在该回合已结束时做：长工具执行期间可能长时间无输出，
        # 但回合没结束就不算闲置（in_turn 保护）
        if not slot.get("in_turn") and now - slot.get("last_event", now) > SESSION_IDLE_TIMEOUT:
            # 智能回收：回合虽已结束，但 claude 名下还有活跃子进程（后台编译/巡检
            # 等 run_in_background 任务）时推迟回收，长任务不误杀
            active = _active_child_count(proc)
            if active:
                if not slot.get("defer_log"):
                    slot["defer_log"] = True
                    log(f"[闲置回收] 检测到 {active} 个活跃子进程，推迟回收"
                        f"（子进程结束后再收）chat={chat_id[-6:]}")
                continue
            slot["reason"] = "idle"
            try:
                proc.stdin.close()   # 优雅退出：claude 收到 EOF，跑完当前回合自然退出
            except (BrokenPipeError, OSError, ValueError):
                pass
            return


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


# ---------------- 会话摘要（后台小模型生成，/sessions 列表展示） ----------------
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "haiku").strip()   # 摘要用的模型
_summarizing: set = set()            # 生成中的 session_id，防并发重复
_summarize_lock = threading.Lock()


def _session_digest(session_file: Path, max_items: int = 40, max_chars: int = 100) -> str:
    """从转录提取对话时间线（用户消息 + 助手正文，逐条截断），喂给摘要模型。

    工具调用/工具结果/思考块都不取——摘要只要"人说了什么、Claude 答了什么"。"""
    out: list = []
    try:
        with session_file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(out) >= max_items:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("isMeta") or d.get("isSidechain"):
                    continue
                m = d.get("message") or {}
                who = {"user": "用户", "assistant": "助手"}.get(m.get("role"))
                if not who:
                    continue
                content = m.get("content")
                texts = [content] if isinstance(content, str) else (
                    [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
                    if isinstance(content, list) else [])
                t = " ".join(x.strip() for x in texts if x and x.strip())
                if t:
                    entry = f"{who}: {t[:max_chars]}"
                    if not out or out[-1] != entry:   # 转录双写会产生相邻重复行，去重省 token
                        out.append(entry)
    except OSError:
        pass
    return "\n".join(out)


def _generate_summary(sid: str, session_file: Path) -> None:
    """起小模型把会话压成一行摘要。--max-turns 1：纯文本应答，不给它动工具的机会。"""
    digest = _session_digest(session_file)
    if len(digest) < 80:    # 没有实质对话，不值得生成
        return
    prompt = ("下面是一段 Claude Code 会话的对话时间线摘录。用一句中文总结这个会话"
              "主要做了什么（不超过 50 个字，说清对象与结果，不要客套），"
              "直接输出这一句：\n\n" + digest)
    env = {**os.environ, "FEISHU_BOT_CHILD": "1"}   # 别触发桌面通知 hook
    if GIT_BASH:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = GIT_BASH
    try:
        r = subprocess.run(
            [CLAUDE_PATH, "-p", "--model", SUMMARY_MODEL, "--max-turns", "1"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, env=env, cwd=str(BASE_DIR))
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"[摘要] 生成失败 sid={sid[:8]}… {e!r}")
        return
    first = (r.stdout or "").strip().strip('"').splitlines()
    text = first[0].strip() if first else ""
    if r.returncode != 0 or not text:
        log(f"[摘要] 生成失败 sid={sid[:8]}… rc={r.returncode} {(r.stderr or '')[:200]}")
        return
    _summaries[sid] = text[:120]
    save_summaries()
    log(f"[摘要] 已生成 sid={sid[:8]}… {text[:60]}")


def _maybe_summarize(sid) -> None:
    """驻留进程收尾时调用：该会话还没有摘要就后台补一份（每个会话只做一次）。"""
    if not sid or sid in _summaries:
        return
    with _summarize_lock:
        if sid in _summarizing:
            return
        _summarizing.add(sid)
    session_file, _cwd = _find_session_file(sid)

    def _run() -> None:
        try:
            if session_file is not None:
                _generate_summary(sid, session_file)
        finally:
            with _summarize_lock:
                _summarizing.discard(sid)

    threading.Thread(target=_run, daemon=True).start()


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


def _session_tail(session_file: Path, n: int = 14, since: int = -1):
    """转录尾部最近的对话动态 [(时间, 角色, 摘要)]，用于 /peek 旁观。

    工具结果（📤）也展示——桌面会话的大量活动是命令输出，不展示等于丢内容。
    🤖 文字回复全显不截（观察挂监控就是看它说话）；工具类保持简略：
    输出截 600 字、用户文本截 1000 字，帧内条目多时工具类自适应收紧防超卡。
    /peek 看进度，要全文就 /takeover 接过去。
    since >= 0 为增量模式：只收集该字节偏移之后新增的条目（观察帧防丢——
    高速回合里固定 14 条窗口会把中间条目挤出视野，增量保证每条都出现），
    新增不足 n 条时退回尾部快照保持上下文感。"""
    out = []
    try:
        st = session_file.stat()
        with session_file.open("rb") as fh:
            if since >= 0:
                # 回退少许再跳过第一行：偏移点可能落在某行中间，首行可能是
                # 截半的（解析自然失败）；即便是完整行也属上帧已见范围
                fh.seek(max(0, since - 64))
                data = fh.read().decode("utf-8", "replace")
                nl = data.find("\n")
                data = data[nl + 1:] if nl >= 0 else ""
            else:
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
            ts_raw = d.get("timestamp") or ""
            ts = ts_raw[11:19]
            if ts_raw.endswith("Z"):   # 转录时间戳是 UTC：转成北京时间（UTC+8）再展示
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) \
                        .astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
                except ValueError:
                    pass
            c = (d.get("message") or {}).get("content")
            if t == "user":
                if d.get("isMeta"):
                    continue
                if isinstance(c, str):
                    if c.strip():
                        out.append((ts, "👤", c.strip().replace("\n", " ")[:1000]))
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        for b in c:   # 工具结果：桌面会话的主要"输出"
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                rc = b.get("content")
                                texts = [rc] if isinstance(rc, str) else (
                                    [x.get("text", "") for x in rc
                                     if isinstance(x, dict) and x.get("type") == "text"]
                                    if isinstance(rc, list) else [])
                                txt = " ".join(x.strip() for x in texts if x and x.strip())
                                if txt:
                                    mark = "📤❌" if b.get("is_error") else "📤"
                                    out.append((ts, mark, txt.replace("\n", " ")[:600]))
                    else:
                        txts = [b["text"].strip() for b in c
                                if isinstance(b, dict) and b.get("type") == "text"
                                and (b.get("text") or "").strip()]
                        if txts:
                            out.append((ts, "👤", " ".join(txts).replace("\n", " ")[:1000]))
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        # 文字回复全显不截，且保留换行——压平会把 markdown 表格的
                        # 行结构摧毁（一行竖线串渲染不出表格），观察卡就看不到表了
                        out.append((ts, "🤖", b["text"].strip()))
                    elif b.get("type") == "tool_use":
                        out.append((ts, "🔧", _tool_summary(b.get("name", ""), b.get("input"))))
        if since >= 0 and len(out) < n:
            return _session_tail(session_file, n)   # 新增不足一屏：退回快照
        if since >= 0:
            # 增量帧可能攒了很多条：工具类条目越多截越紧防卡片超限，🤖 文字回复全显不收
            per = 600 if len(out) <= 30 else 300
            out = [(t, w, txt[:per] + "…" if w != "🤖" and len(txt) > per else txt)
                   for t, w, txt in out]
            return out
        return out[-n:]
    except OSError:
        return []


def _peek_lines(chat_id: str, sid: str, sf: Path, cwd, eye: str = "👀",
                since: int = -1) -> list:
    """/peek 与 /observe 共用的进度渲染：状态头 + 最近动态 + 归属提示。
    since >= 0 时动态区改增量渲染（上帧水位之后的新条目全显，防丢帧）。"""
    state, age = _session_state(sf)
    proj = Path(cwd).name if cwd else "?"
    name = _names.get(sid)
    if state == "running":
        head = f"{eye} [{proj}] {sid[:8]}… ⚡回合进行中（{int(age)} 秒前仍在写入）"
    elif state == "idle":
        head = f"{eye} [{proj}] {sid[:8]}… 💤空闲（最后活动 {_rel_time(sf.stat().st_mtime)}）"
    else:
        head = f"{eye} [{proj}] {sid[:8]}…（状态未知）"
    if name:
        head += f" 📛{name}"
    lines = [head, "最近动态："]
    for t, who, txt in _session_tail(sf, since=since):
        if "\n" in txt:
            # 多行内容（含表格）：给每行压 "时间 角色" 前缀会污染行首，表格就渲染
            # 不出来了——元信息独立成行，原文整块另起（前后空行兼顾表格的上下文）
            lines += [f"  {t} {who} ⬇", "", txt, ""]
        else:
            lines.append(f"  {t} {who} {txt}")
    pp_line = _pending_perm_line(sid, sf)
    if pp_line:
        lines.insert(1, pp_line)
    # 尾部提示按会话归属区分：自己的会话别喊"接管"，别人的要注明会挤掉对方
    with _sessions_lock:
        owner = next((c for c, v in _sessions.items()
                      if isinstance(v, dict) and v.get("session_id") == sid), None)
    if owner == chat_id:
        lines.append("\nℹ️ 这是本聊天当前绑定的会话——直接发消息即可继续（旁观不打扰）。")
    elif owner:
        lines.append(f"\n/takeover {sid} ← 接管（注意：该会话正绑定在另一个飞书聊天，"
                     "接管会转移所有权，对方会收到通知）")
    else:
        lines.append(f"\n/takeover {sid} ← 接管（正在跑的会话会自动排队，跑完接续）")
    return lines


# ---------------- 实时观察（/observe：盯转录文件，新动静时节流刷新快照卡片） ----------------
OBSERVE_FILE = BASE_DIR / "observe.json"
OBSERVE_REFRESH = int(os.environ.get("OBSERVE_REFRESH_SECONDS", "15"))      # 快照刷新最小间隔（秒）
_observers_lock = threading.Lock()
_observers: dict = {}    # chat_id -> {"sid": str, "stop": threading.Event}
_observe_saved: dict = {}
if OBSERVE_FILE.exists():
    try:
        _observe_saved = json.loads(OBSERVE_FILE.read_text("utf-8"))
    except Exception:
        _observe_saved = {}


def _save_observers() -> None:
    with _observers_lock:
        data = {c: v["sid"] for c, v in _observers.items() if isinstance(v, dict)}
    try:
        OBSERVE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def _stop_observer(chat_id: str, note: str = "", sid: str = None) -> bool:
    """停止本聊天的观察（幂等）。sid 非空时只停对该会话的观察（换绑别的会话不动它）。
    note 非空则给聊天回执一条。返回 True=确实在观察且已停。"""
    with _observers_lock:
        obs = _observers.get(chat_id)
        if obs is None or (sid is not None and obs.get("sid") != sid):
            return False
        del _observers[chat_id]
        obs["stop"].set()
    _save_observers()
    if note:
        send_text(chat_id, note)
    return True


def _observe_loop(chat_id: str, sid: str, stop: threading.Event) -> None:
    """观察线程：盯会话转录文件，有新动静且距上帧够久 → 整卡刷新快照卡片。

    卡片内容 = /peek 的完整渲染，一张卡反复 patch（patch 失败重建新卡，再失败
    退回文本消息，内容不丢）。转录文件消失也结束；闲置不自动停（挂监控场景
    可以长期盯）。观察是纯只读旁观——不占用会话，多个聊天可以各自观察
    同一个会话，与排队接管、驻留进程的流式卡片互不干扰。"""
    sf, cwd = _find_session_file(sid)
    if sf is None:
        send_text(chat_id, f"📡 找不到会话 {sid[:8]}… 的转录文件，观察未开始。")
        with _observers_lock:
            _observers.pop(chat_id, None)
        _save_observers()
        return
    msg_id = None
    last_size = -1
    last_push = 0.0

    def _cleanup() -> None:
        with _observers_lock:
            _observers.pop(chat_id, None)
        _save_observers()

    def _frame(note: str) -> None:
        """推一帧快照：记下文件水位，渲染 /peek 式内容并整卡更新。
        动态区增量渲染（since=上帧水位）：高速回合 15 秒节流窗内写出的
        全部条目都会进这一帧，不再被 14 条快照窗口挤丢。"""
        nonlocal msg_id, last_size, last_push
        prev = last_size
        try:
            last_size = sf.stat().st_size
        except OSError:
            pass
        last_push = time.time()
        content = "\n".join(_peek_lines(chat_id, sid, sf, cwd, eye="📡", since=prev))
        # 卡片 2.0 + markdown 组件（1.0 的元素实测不渲染）；状态行并入内容尾部斜体
        card = {"schema": "2.0",
                "body": {"elements": [{"tag": "markdown",
                                       "content": _md_tables_need_blank(content or "…") + f"\n\n*({note})*"}]}}
        if msg_id:   # 常规路径：整卡刷新；失败降级为重建新卡
            try:
                req = PatchMessageRequest.builder() \
                    .message_id(msg_id) \
                    .request_body(PatchMessageRequestBody.builder()
                                  .content(json.dumps(card, ensure_ascii=False))
                                  .build()).build()
                resp = api.im.v1.message.patch(req)
                if resp.success():
                    return
                log(f"[观察卡片更新失败] code={resp.code} msg={resp.msg}")
            except Exception as e:
                log(f"[观察卡片更新异常] {e!r}")
            msg_id = None
        msg_id = _send_card(chat_id, card)
        if msg_id is None:
            send_text(chat_id, content)   # 卡片通道不可用：文本兜底，内容不丢

    _frame(f"📡 实时观察中 · 有动静约每 {OBSERVE_REFRESH} 秒刷新一帧 · /unobserve 停止")
    while not stop.is_set():
        stop.wait(3)
        if stop.is_set():
            return   # /unobserve、换目标或接管自动停：回执由发起方发
        try:
            st = sf.stat()
        except OSError:
            send_text(chat_id, f"📡 会话 {sid[:8]}… 的转录文件已消失，观察结束。")
            _cleanup()
            return
        if st.st_size != last_size:
            if time.time() - last_push >= OBSERVE_REFRESH:
                _frame("📡 实时观察中 · /unobserve 停止")


def _start_observer(chat_id: str, sid: str) -> None:
    """开始/切换观察目标：停旧的，落盘 observe.json，起观察线程（首帧立即推）。"""
    with _observers_lock:
        old = _observers.pop(chat_id, None)
        if old:
            old["stop"].set()
        stop = threading.Event()
        _observers[chat_id] = {"sid": sid, "stop": stop}
    _save_observers()
    threading.Thread(target=_observe_loop, args=(chat_id, sid, stop), daemon=True).start()
    log(f"[实时观察] chat={chat_id[-6:]} → 会话 {sid[:8]}…")


# ---------------- Provider 供应商切换（cc-switch 库：额度耗尽时飞书直接换 key） ----------------
CCSWITCH_DIR = Path.home() / ".cc-switch"
CCSWITCH_DB = CCSWITCH_DIR / "cc-switch.db"
CCSWITCH_SETTINGS = CCSWITCH_DIR / "settings.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _providers() -> list:
    """cc-switch 库里的 Claude 供应商 [{"id","name","base","key_tail","is_current","env"}]。
    env 为空 dict 表示官方 OAuth 等非 env 形态（bot 切不了，得走 cc-switch 界面）。"""
    try:
        db = sqlite3.connect(f"file:{CCSWITCH_DB.as_posix()}?mode=ro", uri=True)
        rows = db.execute(
            "select id,name,settings_config,is_current,sort_index from providers "
            "where app_type='claude' order by sort_index").fetchall()
        db.close()
    except Exception:
        return []
    out = []
    for pid, name, cfg, cur, _si in rows:
        try:
            env = (json.loads(cfg) or {}).get("env") or {}
        except Exception:
            env = {}
        key = str(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or "")
        out.append({"id": pid, "name": (name or "?").strip(), "env": env,
                    "base": env.get("ANTHROPIC_BASE_URL") or "?",
                    "key_tail": key[-4:] if len(key) >= 8 else "?",
                    "is_current": bool(cur)})
    return out


def _provider_now() -> str:
    """settings.json 当前生效的供应商一行描述（key 只显尾号；db 里对不上时如实标注）。"""
    try:
        env = json.loads(CLAUDE_SETTINGS.read_text("utf-8")).get("env") or {}
    except Exception:
        return "未知（读不到 ~/.claude/settings.json）"
    key = str(env.get("ANTHROPIC_API_KEY") or "")
    base = str(env.get("ANTHROPIC_BASE_URL") or "?")
    tail = key[-4:] if len(key) >= 8 else "?"
    hit = next((p for p in _providers()
                if p["key_tail"] == tail and p["base"] == base), None)
    name = hit["name"] if hit else "(不在 cc-switch 库中)"
    return f"{name} · {base} · 模型 {env.get('ANTHROPIC_MODEL') or '?'} · key …{tail}"


def _session_model(sf: Path) -> str:
    """转录里最近一次实际使用的模型（尾部 assistant 条目的 model 字段）。"""
    try:
        with sf.open("rb") as fh:
            fh.seek(max(0, sf.stat().st_size - 262144))
            for line in reversed(fh.read().decode("utf-8", "replace").splitlines()):
                if '"model"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = (d.get("message") or {}).get("model")
                if m:
                    return m
    except OSError:
        pass
    return "?"


def _gateway_models(base: str, key: str) -> list:
    """网关 /v1/models 模型清单（5 秒超时；失败返回空，调用方降级提示）。"""
    if not base:
        return []
    req = urllib.request.Request(base.rstrip("/") + "/v1/models",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return []


def _current_endpoint() -> tuple:
    """settings.json 生效中的 (base_url, key)——网关模型查询用。"""
    try:
        env = json.loads(CLAUDE_SETTINGS.read_text("utf-8")).get("env") or {}
        return env.get("ANTHROPIC_BASE_URL") or "", str(env.get("ANTHROPIC_API_KEY") or "")
    except Exception:
        return "", ""


def _model_chain() -> list:
    """模型决定链（settings.json）：顶层 model（别名展开）→ env 默认，一层一条。"""
    try:
        s = json.loads(CLAUDE_SETTINGS.read_text("utf-8"))
    except Exception:
        return []
    env = s.get("env") or {}
    out = []
    m = s.get("model")
    if m:
        if str(m).lower() in ("opus", "sonnet", "haiku"):
            real = env.get(f"ANTHROPIC_DEFAULT_{str(m).upper()}_MODEL") or "?"
            out.append(f'model="{m}" → 别名映射 {real}')
        else:
            out.append(f'model="{m}"（直接指定）')
    if env.get("ANTHROPIC_MODEL"):
        out.append(f"env 默认 {env['ANTHROPIC_MODEL']}")
    return out


def _recycle_idle_children() -> tuple:
    """回收所有空闲分身（换 provider/模型后，新配置只对起新进程生效；
    空闲分身内存里揣着旧配置）。返回 (已回收数, 忙碌保留数)。"""
    idle = []
    busy = 0
    with _running_lock:
        for v in list(_running.values()):
            if v.get("in_turn"):
                busy += 1
            else:
                idle.append(v)
    for v in idle:
        v["reason"] = "recycle"
        if v.get("proc"):
            kill_task(v["proc"])
    return len(idle), busy


def _switch_provider(chat_id: str, arg: str) -> None:
    """切换供应商：整块换 settings.json 的 env（其余配置不动）+ 同步 cc-switch 两处
    状态保持 GUI 一致；回收空闲分身（内存里揣着旧 key；绑定保留，下条消息自动
    --resume 起新进程用新 key）。正在跑回合的分身不硬杀，回执说明。"""
    provs = _providers()
    if not provs:
        send_text(chat_id, "❌ 读不到 cc-switch 供应商库（~/.cc-switch/cc-switch.db）。")
        return
    a = arg.strip().lower()
    target = provs[int(a) - 1] if a.isdigit() and 1 <= int(a) <= len(provs) else None
    if target is None:
        cands = [p for p in provs if a and a in p["name"].lower()]
        if len(cands) == 1:
            target = cands[0]
    if target is None:
        send_text(chat_id, "用法：/provider <序号|名称关键字>（不带参数列出全部供应商）。")
        return
    if not target["env"]:
        send_text(chat_id, f"❌ {target['name']} 是官方 OAuth 等非 env 形态配置，"
                           "bot 切不了，请在 cc-switch 界面切换。")
        return
    try:
        settings = json.loads(CLAUDE_SETTINGS.read_text("utf-8"))
    except Exception:
        send_text(chat_id, "❌ 读不到 ~/.claude/settings.json，未做任何改动。")
        return
    old_env = settings.get("env") or {}
    if target["base"] == old_env.get("ANTHROPIC_BASE_URL") and \
            target["key_tail"] == str(old_env.get("ANTHROPIC_API_KEY") or "")[-4:]:
        send_text(chat_id, f"✅ {target['name']} 已经是当前供应商，无需切换。")
        return
    # 1) ~/.claude/settings.json：整块换 env（hooks/permissions 等其余配置不动）
    log(f"[Provider 切换] {old_env.get('ANTHROPIC_BASE_URL')} → {target['base']}")
    settings["env"] = dict(target["env"])
    CLAUDE_SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    # 2) cc-switch 两处状态（GUI 显示一致；SQLite 短事务，与界面并发安全）
    try:
        db = sqlite3.connect(f"file:{CCSWITCH_DB.as_posix()}", uri=True)
        db.execute("update providers set is_current=0 where app_type='claude'")
        db.execute("update providers set is_current=1 where id=?", (target["id"],))
        db.commit()
        db.close()
        cs = json.loads(CCSWITCH_SETTINGS.read_text("utf-8"))
        cs["currentProviderClaude"] = target["id"]
        CCSWITCH_SETTINGS.write_text(json.dumps(cs, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        log(f"[Provider 状态同步失败，settings.json 已切换] {e!r}")
    # 3) 清聊天级模型（模型名跨网关不通用）+ 回收空闲分身（揣着旧 key）
    with _models_lock:
        cleared = len(_chat_models)
        _chat_models.clear()
    save_models()
    idle, busy = _recycle_idle_children()
    tips = [f"🔄 供应商已切换：{target['name']}（{target['base']}）",
            "· 新起进程立即用新 key；终端里的活会话需重启 resume 才换"]
    if cleared:
        tips.append(f"· 已清除 {cleared} 个聊天的模型设置（模型名跨网关不通用，/model 重选）")
    if idle:
        tips.append(f"· 已回收空闲分身 {idle} 个（绑定保留，发消息自动续接）")
    if busy:
        tips.append(f"· ⚠️ {busy} 个分身正在跑回合，这轮跑完仍是旧 key")
    send_text(chat_id, "\n".join(tips))


# ---------------- 命令缩写（/ob → /observe：唯一前缀展开，歧义则提示补全） ----------------
COMMANDS = ["/help", "/new", "/exit", "/stop", "/status", "/session", "/sessions",
            "/takeover", "/peek", "/observe", "/unobserve", "/unqueue", "/rename",
            "/cwd", "/mode", "/model", "/mute", "/unmute", "/quiet", "/unquiet",
            "/listen", "/provider", "/handover"]


def _expand_cmd(text: str):
    """首词若是不完整的命令缩写则展开。返回 (text, note)：text=None 表示歧义终止，
    note 是回执提示。完整命令与自定义命令原样放行。"""
    parts = text.split(None, 1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if head in COMMANDS or not head.startswith("/") or len(head) < 2:
        return text, ""
    hits = [c for c in COMMANDS if c.startswith(head)]
    if len(hits) == 1:
        return hits[0] + ((" " + rest) if rest else ""), f"↔️ {head} → {hits[0]}"
    if hits:
        return None, f"❓ {head} 匹配多个命令：{' '.join(hits)}（多打几位消歧义）"
    return text, ""   # 未知命令，交原逻辑


# ---------------- 命令处理 ----------------
def handle_command(chat_id: str, text: str) -> None:
    global WORK_DIR
    text, note = _expand_cmd(text)
    if note:
        send_text(chat_id, note)
    if text is None:
        return
    parts = text.split()
    cmd = parts[0].lower()
    arg = text[len(parts[0]):].strip()

    if cmd == "/help":
        if not _send_card(chat_id, _info_card("📖 命令帮助", HELP_MD)):
            send_text(chat_id, HELP_TEXT)   # 卡片发送失败兜底纯文本
    elif cmd == "/new":
        old_sid = (_sessions.get(chat_id) or {}).get("session_id")
        if old_sid:
            _remember_last(chat_id, old_sid)   # 旧会话让位，/takeover last 可接回
        _recycle_chat_sessions(chat_id)   # 终结旧会话的驻留进程
        with _pending_lock:   # 排队接管一并取消（否则队列线程稍后会发给新会话）
            _pending.pop(chat_id, None)
        with _sessions_lock:
            _sessions.pop(chat_id, None)
        save_sessions()
        send_text(chat_id, "🆕 已新开零上下文会话，下一条消息从零开始"
                           "（旧会话历史保留，/takeover last 可接回）。")
    elif cmd == "/exit":
        info = _sessions.get(chat_id)
        if not info:
            send_text(chat_id, "当前聊天没有绑定会话，无需退出（直接发消息会开新会话）。")
            return
        sid = info.get("session_id") or ""
        if sid:
            _remember_last(chat_id, sid)   # /takeover last 可一键接回
        _recycle_chat_sessions(chat_id, reason="exit")   # 回收本聊天的驻留进程
        with _pending_lock:   # 排队接管一并取消
            _pending.pop(chat_id, None)
        with _sessions_lock:
            _sessions.pop(chat_id, None)
        save_sessions()
        label = _names.get(sid) or f"{sid[:8]}…"
        send_text(chat_id, f"🚪 已退出会话 {label}（驻留进程已回收，历史保留）。\n"
                           "· /takeover last 可随时重新接管本会话\n"
                           "· 直接发消息则开启全新会话")
    elif cmd == "/handover":
        # 一键移交终端：分身先收尾（总结写进转录，终端接手第一眼可见），再退出+解绑。
        # 正在跑的回合直接中断——"剩下的活"正是要交给终端的部分。
        sess = _sessions.get(chat_id) or {}
        sid = sess.get("session_id")
        if not sid:
            send_text(chat_id, "当前聊天没有绑定会话，无需移交"
                               "（终端里 claude --resume <ID> 可接任意会话）。")
            return
        note = " ".join(arg.split())   # 移交说明压成单行，引号换成中文引号防破坏命令行
        note = note.replace('"', "”")
        cwd = sess.get("work_dir") or _chat_work_dir(chat_id)
        if note:   # 带说明：嵌进接手命令，终端粘贴回车即带着说明续跑
            cmd_line = f'claude --resume {sid} "【飞书移交】剩下的活继续：{note}"'
        else:
            cmd_line = f"claude --resume {sid}"
        # 正在跑的回合先中断（记 handover 原因，读泵静默收尾）
        with _running_lock:
            targets = [(k, s) for k, s in _running.items()
                       if s.get("chat_id") == chat_id and s.get("proc") is not None
                       and s.get("in_turn")]
        for key, slot in targets:
            slot["reason"] = "handover"
            kill_task(slot["proc"])
            with _msg_queue_lock:
                _msg_queue.pop(key, None)
        # 挂移交单：收尾回合结束（_finish_turn）或超时（Timer）由 _finalize 落地
        with _handover_lock:
            _handovers[chat_id] = {"sid": sid, "cmd_line": cmd_line, "ts": time.time()}
        t = threading.Timer(HANDOVER_WAIT, _finalize_handover, args=(chat_id, True))
        t.daemon = True
        t.start()
        # 收尾指令：进程活着（空闲）直送 stdin；被中断/本无进程则 run_task --resume 重生
        wrapup = ("【移交终端】本会话即将从飞书移交电脑终端，请立即收尾、不要开始新的工作："
                  "简要总结 ①当前任务进行到哪一步 ②已完成的关键改动/结论"
                  f"③剩下的活（移交说明：{note or '见上文'}）④终端接手时的注意事项"
                  "⑤清点你拉起的后台任务（run_in_background 的监控脚本等）：逐个说明"
                  "任务内容、脚本路径、原样启动命令、输出位置、最近一次结果——"
                  "注意：若这些任务随上一回合的中断而停止，接管方会按此清单重启并继续跟进。"
                  "这段总结将留在会话转录里，是终端接手后读到的最后内容。")
        run_task(chat_id, wrapup)
        send_text(chat_id, "🤝 移交中：分身正在做收尾总结（写进转录，终端接手可见），"
                           "完成后自动退出并发送接手命令。\n"
                           f"· 工作目录：{cwd}\n"
                           f"· 接手命令（完成后自动进剪贴板）：{cmd_line}")
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
        with _listen_lock:
            listening = chat_id in _listen
        lines = [
            "**⚙️ 本聊天设置**",
            "- 权限模式：" + (f"{_mode_label(chat_id)}（本聊天独立，/mode 切换）" if chat_id in _modes
                            else f"{_mode_label(chat_id)}（全局默认，/mode 可单独设置本聊天）"),
            "- 静默模式：" + ("开启——只推权限卡片/最终结果/异常（/unquiet 关闭）" if chat_id in _quiet
                             else "关闭（/quiet 开启：不刷思考/工具过程）"),
            "- 桌面事件推送：" + ("已静音" if (BASE_DIR / "mute.flag").exists() else "正常")
                             + "（优先发到与该会话相关的聊天——接管/让位记录；无关会话发最后活跃聊天）",
        ]
        if listening:
            lines.append("· 群免 @：本群已开启（/listen 关闭）")
        if CLAUDE_TIMEOUT > 0:
            lines.append(f"· 单回合限时：{CLAUDE_TIMEOUT}s（超时强杀，/stop 随时中断）")
        label = _chat_label(chat_id)
        lines += ["", "**🧠 本聊天的 Claude 会话**",
                  f"- 会话：{label[1:-1] if label else '未绑定（下一条消息开新会话）'}",
                  f"- 工作目录：{_chat_work_dir(chat_id)}"]
        cur_key = _task_key(chat_id)
        if slots:
            for k, v in slots:
                who = "当前会话" if k == cur_key else f"{k[:8]}…"
                idle_s = int(time.time() - v.get("last_event", v["started"]))
                if v.get("in_turn"):
                    lines.append(f"- 进程状态：🔴 {who} 工作中（最近活动 {idle_s}s 前；/stop 可中断）")
                else:
                    lines.append(f"- 进程状态：🟢 {who} 空闲驻留（{idle_s}s 无活动，"
                                 f"{SESSION_IDLE_TIMEOUT // 60} 分钟后自动回收，消息直达进程）")
        else:
            lines.append("- 进程状态：⚪ 无驻留进程（发消息自动 --resume 续接上下文）")
        if qn:
            lines.append(f"- 📥 排队中：{qn} 条消息（自动依次执行）")
        if pend:
            lines.append(f"- ⏳ 排队接管：等会话 {pend['sid'][:8]}… 空闲（已积 {len(pend['queue'])} 条；/unqueue 取消）")
        obs_sid = (_observers.get(chat_id) or {}).get("sid")
        if obs_sid:
            lines.append(f"- 📡 实时观察：会话 {obs_sid[:8]}…（快照卡片自动刷新，/unobserve 停止）")
        lines += ["", "**🔑 Provider**",
                  f"- 全局生效：{_provider_now()}"]
        m_set = _chat_models.get(chat_id)
        lines.append(f"- 本聊天模型：{m_set}（显式）" if m_set else "- 本聊天模型：跟随全局")
        sess = _sessions.get(chat_id) or {}
        if sess.get("session_id"):
            sf, _cwd2 = _find_session_file(sess["session_id"])
            if sf:
                born = next((v.get("provider") for k, v in slots if v.get("provider")), None)
                extra = f"（驻留分身启动于 {born}）" if born else "（终端会话按其启动时刻的配置）"
                lines.append(f"- 本会话最近模型：{_session_model(sf)} {extra}")
        md = "\n".join(lines)
        if not _send_card(chat_id, _info_card("📊 状态面板", md, "turquoise")):
            send_text(chat_id, md)   # 卡片发送失败兜底纯文本
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
        if want.lower() == "last":
            # 本聊天上一次让位/移交出去的会话（/exit /handover 换绑时自动记录）
            info = _last_sessions.get(chat_id) or {}
            sid = info.get("sid")
            if not sid:
                send_text(chat_id, "本聊天没有让位过的会话记录"
                                   "（/exit、/handover 或换绑后可 /takeover last 接回）。")
                return
            cwd = info.get("work_dir")
            if not cwd:   # 记录时没带目录：从会话列表兜底
                _, cwd = _find_session_file(sid)
        elif want.lower() == "latest":
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
        old_sid = (_sessions.get(chat_id) or {}).get("session_id")
        if old_sid and old_sid != sid:
            _remember_last(chat_id, old_sid)   # 换绑前的旧会话，/takeover last 可接回
        _recycle_chat_sessions(chat_id)   # 换绑：旧会话的驻留进程让位
        evicted = _bind_session(chat_id, sid, cwd)
        _mark_terminal_handoff(sid)   # 终端宿主跑完回合 Stop 时会被劝停交出
        stolen = "\n📌 该会话此前绑定在另一个聊天，已转移过来（对方已收到通知）。" if evicted else ""
        send_text(chat_id,
                  f"✅ 已接管会话 {sid[:8]}…（目录：{_chat_work_dir(chat_id)}）\n"
                  "下一条消息将带上该会话的全部上下文。\n"
                  "· 已给终端留移交指令：它跑完手头回合会自动收尾交出"
                  "（含后台任务台账），之后不再接活。" + stolen)
    elif cmd == "/peek":
        want = arg.strip()
        sid = None
        if not want:
            sid = (_sessions.get(chat_id) or {}).get("session_id")
            if not sid:
                send_text(chat_id, "当前聊天没有绑定会话。用法：/peek <序号|会话ID|latest|last>")
                return
        elif want.lower() == "last":
            # 本聊天上一次让位/移交出去的会话（/exit /handover 换绑时自动记录）
            sid = (_last_sessions.get(chat_id) or {}).get("sid")
            if not sid:
                send_text(chat_id, "本聊天没有让位过的会话记录"
                                   "（/exit、/handover 或换绑后可 /peek last 查看）。")
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
        send_text(chat_id, "\n".join(_peek_lines(chat_id, sid, sf, cwd2)))
    elif cmd == "/observe":
        want = arg.strip()
        sid = None
        if not want:
            sid = (_sessions.get(chat_id) or {}).get("session_id")
            if not sid:
                send_text(chat_id, "当前聊天没有绑定会话。用法：/observe <序号|会话ID|latest|last>")
                return
        elif want.lower() == "last":
            # 本聊天上一次让位/移交出去的会话（/exit /handover 换绑时自动记录）
            sid = (_last_sessions.get(chat_id) or {}).get("sid")
            if not sid:
                send_text(chat_id, "本聊天没有让位过的会话记录"
                                   "（/exit、/handover 或换绑后可 /observe last 观察）。")
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
        with _observers_lock:
            cur = (_observers.get(chat_id) or {}).get("sid")
        if cur == sid:
            # 重复观察同一会话 = 重发新卡：中途聊天把观察卡顶远了，/ob 一下
            # 让最新的快照卡落回聊天底部（旧卡定格保留，刷新转移到新卡）
            _start_observer(chat_id, sid)   # 停旧线程起新的，新线程立即推首帧
            send_text(chat_id, f"🔄 观察卡已重发：会话 {sid[:8]}…（最新动态见新卡，/unobserve 停止）")
            return
        _start_observer(chat_id, sid)   # 首帧由观察线程立即推出
        extra = f"\n（观察目标已从 {cur[:8]}… 切换）" if cur else ""
        send_text(chat_id, f"📡 开始实时观察会话 {sid[:8]}…{extra}\n"
                           f"· 快照卡片随转录动静自动刷新（约 {OBSERVE_REFRESH} 秒一帧）\n"
                           "· 长期闲置也不自动结束（挂监控场景可长期盯），/unobserve 停止\n"
                           "· 只读旁观：不占用会话，跟排队接管、其他聊天的观察互不干扰")
    elif cmd == "/unobserve":
        if not _stop_observer(chat_id, note="📴 已停止实时观察。"):
            send_text(chat_id, "本聊天当前没有在观察任何会话（/observe <序号|ID|latest|last> 开始）。")
    elif cmd == "/provider":
        provs = _providers()
        if not provs:
            send_text(chat_id, "❌ 读不到 cc-switch 供应商库（~/.cc-switch/cc-switch.db）。")
        elif arg:
            _switch_provider(chat_id, arg)
        else:
            try:
                cur_env = json.loads(CLAUDE_SETTINGS.read_text("utf-8")).get("env") or {}
                cur_tail = str(cur_env.get("ANTHROPIC_API_KEY") or "")[-4:]
                cur_base = cur_env.get("ANTHROPIC_BASE_URL")
            except Exception:
                cur_tail, cur_base = None, None
            lines = ["🔑 Claude 供应商（cc-switch 库）："]
            for i, p in enumerate(provs, 1):
                mark = " ← 生效中" if (p["key_tail"] == cur_tail and p["base"] == cur_base) else ""
                lines.append(f"{i}. {p['name']} · {p['base']} · key …{p['key_tail']}{mark}")
            lines += ["", f"当前生效：{_provider_now()}",
                      "切换：/provider <序号|名称关键字>（新起进程生效，终端活会话需重启）"]
            send_text(chat_id, "\n".join(lines))
    elif cmd == "/model":
        a = arg.strip()
        if a.lower() in ("reset", "default", "clear"):
            with _models_lock:
                old = _chat_models.pop(chat_id, None)
            save_models()
            if not old:
                send_text(chat_id, "本聊天未单独设置模型（本来就在跟随全局）。")
            else:
                idle, busy = _recycle_idle_children()
                tips = [f"↩️ 本聊天模型已重置：{old} → 跟随全局",
                        "· 全局链路：" + (" ｜ ".join(_model_chain()) or "?")]
                if idle:
                    tips.append(f"· 已回收空闲分身 {idle} 个（绑定保留，发消息自动续接）")
                if busy:
                    tips.append(f"· ⚠️ {busy} 个分身正在跑回合，这轮跑完仍是旧模型")
                send_text(chat_id, "\n".join(tips))
        elif a:
            # 目标：网关序号（需查询成功）或名称直接采纳（网关不支持列表时也能设）
            target = None
            base, key = _current_endpoint()
            glist = _gateway_models(base, key)
            if a.isdigit() and glist:
                if 1 <= int(a) <= len(glist):
                    target = glist[int(a) - 1]
                else:
                    send_text(chat_id, f"序号超出范围（网关列表共 {len(glist)} 个）。")
                    return
            if target is None:
                target = a
                if glist and target not in glist:
                    send_text(chat_id, f"⚠️ {target} 不在网关列表中，仍按名称设置"
                                       "（若模型名有误会由下一轮请求报错，届时 /model 重选）。")
            with _models_lock:
                _chat_models[chat_id] = target
            save_models()
            idle, busy = _recycle_idle_children()
            tips = [f"🧬 本聊天模型已设置：{target}（分身显式 --model，优先于一切别名）",
                    "· 新起分身立即生效；终端会话不受影响"]
            if idle:
                tips.append(f"· 已回收空闲分身 {idle} 个（绑定保留，发消息自动续接）")
            if busy:
                tips.append(f"· ⚠️ {busy} 个分身正在跑回合，这轮跑完仍是旧模型")
            send_text(chat_id, "\n".join(tips))
        else:
            base, key = _current_endpoint()
            glist = _gateway_models(base, key)
            m_set = _chat_models.get(chat_id)
            lines = ["🧬 模型"]
            lines.append(f"· 本聊天设置：{m_set}（显式 --model）" if m_set
                         else "· 本聊天设置：未设置（跟随全局链路）")
            chain = _model_chain()
            if chain:
                lines.append("· 全局链路：" + " ｜ ".join(chain))
            sess = _sessions.get(chat_id) or {}
            if sess.get("session_id"):
                sf, _cwd2 = _find_session_file(sess["session_id"])
                if sf:
                    lines.append(f"· 分身转录实测：{_session_model(sf)}")
            if glist:
                lines.append(f"· 网关可用模型（{base}）：")
                for i, m in enumerate(glist, 1):
                    mark = " ← 本聊天" if m == m_set else ""
                    lines.append(f"  {i}. {m}{mark}")
            else:
                lines.append("· 网关模型清单查询失败（超时/不支持）——可 /model <模型名> 直接指定")
            lines += ["", "切换：/model <序号|名称>  ·  重置：/model reset"]
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
            summary = _summaries.get(it["session_id"])
            if summary:
                lines.append(f"   📝 {summary}")
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
    elif cmd == "/quiet":
        _quiet.add(chat_id)
        save_quiet()
        send_text(chat_id, "🔕 本聊天已开启静默模式：思考过程和工具调用不再实时转发，"
                           "只在需要你拍板时通知——权限确认卡片、每回合的最终结果、异常。"
                           "/unquiet 恢复实时转发（仅本聊天生效，其他聊天不受影响）。")
    elif cmd == "/unquiet":
        _quiet.discard(chat_id)
        save_quiet()
        send_text(chat_id, "🔔 本聊天静默模式已关闭，回复和工具调用恢复实时转发。")
    elif cmd == "/cwd":
        if not arg:
            send_text(chat_id, f"当前工作目录:{_chat_work_dir(chat_id)}\n用法:/cwd D:\\path\\to\\project")
        else:
            p = Path(arg)
            if not p.is_dir():
                send_text(chat_id, f"❌ 目录不存在：{p}")
            else:
                _recycle_chat_sessions(chat_id)   # 目录变了，旧会话进程作废
                WORK_DIR = p.resolve()   # 改全局默认
                with _sessions_lock:
                    _sessions.pop(chat_id, None)  # 只重置本聊天的会话，不影响其他聊天
                save_sessions()
                send_text(chat_id, f"📁 工作目录已切换：{WORK_DIR}\n（本聊天的历史会话已重置）")
    elif cmd == "/mode":
        if not arg:
            cur = _chat_mode(chat_id)
            src = "本聊天独立设置" if chat_id in _modes else "全局默认（.env CLAUDE_PERMISSION_MODE）"
            send_text(chat_id, f"当前权限模式：{cur}（{src}）\n"
                               f"切换：/mode <{' | '.join(sorted(PERMISSION_MODES))}>\n"
                               "对下一条消息生效，仅本聊天（其他聊天不受影响）。")
        elif arg in PERMISSION_MODES:
            _modes[chat_id] = arg
            save_modes()
            # 驻留进程的模式在起进程时就固定：本聊天空闲的旧模式进程立即退役
            # （工作中的等本回合结束自动退役，见 _finish_turn），下条消息用新模式重建
            with _running_lock:
                stale = [s for s in _running.values()
                         if s.get("chat_id") == chat_id
                         and s.get("mode") != _chat_mode(chat_id) and not s.get("in_turn")]
            for s in stale:
                s["reason"] = "recycle"
                try:
                    if s.get("proc") and s["proc"].poll() is None:
                        s["proc"].stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            warn = "\n⚠️ bypassPermissions = 白名单里的人可让 Claude 做任何事，风险自负。" \
                if arg == "bypassPermissions" else ""
            send_text(chat_id, f"✅ 权限模式已切换：{arg}（下一条消息生效，仅本聊天；正在执行的任务"
                               "会在本回合结束后用新模式重建）{warn}")
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
    """卡片按钮回调：待决策卡片（权限确认/接管选择等）的选项点击处理。"""
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
            log(f"[卡片动作] 收到选项点击 chat={chat_id[-6:]} ask={str(req)[-8:]} v={value.get('v')}")
            label = _resolve_choice(chat_id, str(req), value.get("v"))
            if label is None:
                return _card_toast("❌ 该问题已回答或已过期", "error")
            return _card_toast(f"✅ 已选择：{label}")
        # 快捷命令面板（/panel）已下线：旧面板卡片的按钮点击落到这里，明确告知
        return _card_toast("❌ 快捷命令面板已下线，旧卡片按钮不再可用", "error")
    except Exception as e:
        return _card_toast(f"❌ {e!r}", "error")


def _ws_watchdog() -> None:
    """WS 连接看门狗：飞书长连接首连失败/重连卡死时，bot 会"活着但聋"——
    进程在、零出站连接、消息收不到，bot.log 毫无线索（错误只在 SDK 的
    stderr 里）。每 90 秒查一次本进程到外网 443 的 ESTABLISHED 连接，
    连续两轮没有 → 先通知再自杀，把拉起交给外部（restart.ps1 / 保活）。"""
    miss = 0
    while True:
        time.sleep(90)
        try:
            ok = any(c.status == "ESTABLISHED" and c.raddr
                     and c.raddr.ip not in ("127.0.0.1", "::1", "0.0.0.0")
                     and c.raddr.port == 443
                     for c in psutil.Process(os.getpid()).net_connections("tcp"))
        except Exception:
            ok = True   # psutil 探查失败（权限等）：宁可不误杀
        if ok:
            miss = 0
            continue
        miss += 1
        log(f"[WS 看门狗] 未检测到飞书出站连接（第 {miss}/2 次）")
        if miss >= 2:
            log("[WS 看门狗] 持续 3 分钟无飞书连接，判定 WS 卡死，自杀待外部拉起")
            try:
                chat = _desktop_perm_target() or ""
                if chat:
                    send_text(chat, "⚠️ bot 的飞书长连接卡死（进程活着但收不到消息），"
                                    "已自动退出待重启——请稍后发消息验证，或运行 restart.ps1。")
            except Exception:
                pass
            os._exit(1)


def main() -> None:
    if not CLAUDE_PATH:
        sys.exit("❌ 找不到 claude 命令。请确认已安装 Claude Code 并在 PATH 中，"
                 "或在 .env 中设置 CLAUDE_PATH=claude 的完整路径")
    _save_desktop_route()   # 启动即按当前绑定/让位记录重建路由（notify.py 按它推送）
    if not WORK_DIR.is_dir():
        sys.exit(f"❌ 工作目录不存在：{WORK_DIR}")
    valid_modes = PERMISSION_MODES | {""}
    if PERMISSION_MODE not in valid_modes:
        sys.exit(f"❌ CLAUDE_PERMISSION_MODE 无效：{PERMISSION_MODE}\n"
                 f"   可选值：{', '.join(sorted(PERMISSION_MODES))}")
    if GIT_BASH:
        log(f"[Git Bash] 已锁定 {GIT_BASH} → 分身将使用 Bash 工具（白名单 Bash 规则可命中）")
    else:
        log("[Git Bash] 未定位到 bash.exe，分身将使用 PowerShell 工具"
            "（可用 .env 的 GIT_BASH_PATH 指定）")
    if psutil is not None:
        log(f"[智能回收] psutil {psutil.__version__} 已加载（闲置回收前检查活跃子进程）")
    else:
        log("[智能回收] psutil 未安装，退回纯时间判定（请跑 uv sync 安装依赖）")

    # 启动自愈：清理旧版遗留的「同一会话绑多个聊天」脏状态（会话独占规则，
    # 运行期由 _bind_session 的抢占维持）。保留 updated 最新的绑定，其余解绑并通知。
    with _sessions_lock:
        newest = {}   # sid -> (updated, chat_id)
        for chat, info in _sessions.items():
            sid = (info or {}).get("session_id")
            if sid:
                upd = (info or {}).get("updated") or ""
                if sid not in newest or upd > newest[sid][0]:
                    newest[sid] = (upd, chat)
        evicted = [(chat, (info or {}).get("session_id"))
                   for chat, info in _sessions.items()
                   if (info or {}).get("session_id") in newest
                   and newest[(info or {}).get("session_id")][1] != chat]
        for chat, _sid in evicted:
            del _sessions[chat]
    if evicted:
        save_sessions()
        for chat, sid in evicted:
            log(f"[启动自愈] 会话 {sid[:8]}… 重复绑定，保留最新，解绑 chat={chat[-6:]}")
            send_text(chat, f"↪️ 启动检查：会话 {sid[:8]}… 此前同时绑定了多个聊天"
                            "（会话独占规则），本聊天已解绑；"
                            f"/takeover {sid[:8]} 可重新接管。")

    # 恢复重启前的实时观察（先清盘再逐个重启，转录已不存在的记录会被观察线程
    # 首帧回执并自然剔除；成功的由 _start_observer 重新落盘）
    if _observe_saved:
        saved = {c: s for c, s in _observe_saved.items() if isinstance(s, str) and s}
        _observe_saved.clear()
        try:
            OBSERVE_FILE.unlink()
        except OSError:
            pass
        for chat, sid in saved.items():
            _start_observer(chat, sid)

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
    print(f"   权限模式（全局默认）：{PERMISSION_MODE or 'default'}｜超时：{CLAUDE_TIMEOUT if CLAUDE_TIMEOUT > 0 else '不限时'}｜转发工具调用：{'开' if FORWARD_TOOLS else '关'}")
    print(f"   白名单：{len(ALLOWED_USERS)} 人" + ("" if ALLOWED_USERS else "（未配置，首条消息会提示如何配置）"))
    # 权限确认通道：perm_mcp.py（claude 的 MCP 权限回调）连回本进程
    threading.Thread(target=_perm_server, daemon=True).start()
    if psutil:
        threading.Thread(target=_ws_watchdog, daemon=True).start()
    print("   等待消息中…（Ctrl+C 退出）")
    ws.start()  # 阻塞，断线自动重连


if __name__ == "__main__":
    main()
