#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书任务跟踪 MCP 服务器（被 claude 作为 stdio MCP 子进程拉起，所有权限模式挂载）。

给飞书会话的 agent 三个结构化工具，长任务过程跟踪用（11-24 用户批准的场景 1）：
  create_task   开工时建任务（自动挂「Claude 任务」清单、指派用户、标题带会话标签）
  update_task   阶段性进展追加到任务描述（时间线）；不传 guid 默认本会话最近未完成任务
  complete_task 收尾置完成 + 结论写进描述

设计要点（见 bot.py 的 _spawn_claude 注释与本文件工具描述）：
- 独立 server 不绑权限钩子——bypassPermissions 模式同样可用（perm_mcp 只在
  非 bypass 模式挂载，任务工具不能跟着它走）
- 直接调飞书 Task API（tenant token，.env 凭据），不经 bot 进程——bot 重启不影响
- 登记表 data/agent_tasks/<chat_id>.json：本会话任务清单（guid/标题/链接/状态），
  按 chat 分文件避免多会话并发写冲突；原子写（tmp+replace）
- 聊天区只在「创建」「完成」两个低频节点发轻提示，进展更新不刷屏——总览靠
  飞书任务中心的「Claude 任务」清单，聊天区保持任务无关

环境变量（bot.py 拉起 claude 时注入）：
  FEISHU_CHAT_ID    发起任务的飞书聊天（登记表 key + 提示消息路由）
  FEISHU_CHAT_LABEL 聊天显示名（任务标题前缀，如 [msagent·8095050d]）
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TASKLIST_GUID = "ddb3c360-fc8d-4fee-891e-17eadf62e445"   # 「Claude 任务」清单（11-20 建立实测）
REG_DIR = BASE_DIR / "data" / "agent_tasks"

_lark = None        # 模块级缓存的 lark client（进程生命周期内复用）
_env = None


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cfg() -> dict:
    """.env 凭据（手写解析，不引 dotenv）。"""
    global _env
    if _env is None:
        env = {}
        try:
            for ln in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    env[k.strip()] = v.strip()
        except OSError:
            pass
        _env = env
    return _env


def _client():
    global _lark
    if _lark is None:
        import lark_oapi as lark
        cfg = _cfg()
        _lark = (lark.Client.builder()
                 .app_id(cfg.get("FEISHU_APP_ID", ""))
                 .app_secret(cfg.get("FEISHU_APP_SECRET", ""))
                 .build())
    return _lark


def _reg_load(chat_id: str) -> list:
    try:
        d = json.loads((REG_DIR / f"{chat_id}.json").read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _reg_save(chat_id: str, rows: list) -> None:
    try:
        REG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REG_DIR / f"{chat_id}.json.tmp"
        tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, REG_DIR / f"{chat_id}.json")   # 原子写，防读到半截文件
    except OSError:
        pass   # 登记失败不阻塞任务本身——API 任务已建，只是 /tasks 列不到


def _send_text(chat_id: str, text: str) -> None:
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (SendMessageRequest, SendMessageRequestBody)
        req = (SendMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(SendMessageRequestBody.builder()
                             .receive_id(chat_id).msg_type("text")
                             .content(json.dumps({"text": text}, ensure_ascii=False))
                             .build()).build())
        _client().im.v1.message.send(req)
    except Exception:
        pass   # 提示消息失败静默——任务本体已成功，别让通知路径拖垮工具返回


def _resolve_guid(args: dict) -> tuple:
    """工具入参里的 guid：显式传入优先（可操作登记表外的任务）；否则按登记表
    取本会话最近的未完成任务。返回 (guid, summary)；找不到返回 (None, 错误说明)。"""
    guid = (args.get("task_guid") or "").strip()
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if guid:
        hit = next((r for r in _reg_load(chat_id) if r.get("guid") == guid), None)
        return guid, (hit or {}).get("summary", "")
    for r in reversed(_reg_load(chat_id)):
        if (r.get("status") or "todo") == "todo":
            return r.get("guid", ""), r.get("summary", "")
    return None, "本会话没有未完成的已登记任务——先 create_task，或显式传 task_guid"


def _get_task(guid: str):
    from lark_oapi.api.task.v2 import GetTaskRequest
    req = GetTaskRequest.builder().task_guid(guid).user_id_type("open_id").build()
    resp = _client().task.v2.task.get(req)
    if not resp.success():
        raise RuntimeError(f"任务读取失败 code={resp.code} msg={resp.msg}")
    return resp.data.task


def _patch_task(guid: str, fields: list, **kw) -> dict:
    from lark_oapi.api.task.v2 import InputTask, PatchTaskRequest, PatchTaskRequestBody
    it = InputTask.builder()
    for k, v in kw.items():
        getattr(it, k)(v)
    req = (PatchTaskRequest.builder()
           .task_guid(guid).user_id_type("open_id")
           .request_body(PatchTaskRequestBody.builder()
                         .task(it.build()).update_fields(fields).build())
           .build())
    resp = _client().task.v2.task.patch(req)
    if not resp.success():
        raise RuntimeError(f"任务更新失败 code={resp.code} msg={resp.msg}")
    return {"ok": True}


def _now_tag() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def tool_create_task(args: dict) -> dict:
    from lark_oapi.api.task.v2 import InputTask, Member, CreateTaskRequest
    from lark_oapi.api.task.v2 import AddTasklistTaskRequest, AddTasklistTaskRequestBody
    summary = (args.get("summary") or "").strip()
    if not summary:
        return {"error": "summary 不能为空（一句话任务名）"}
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    # 来源标识（11-24 用户定调「来源具体到群聊」）：群聊名优先（bot 注入
    # FEISHU_CHAT_NAME，im.v1.chat.get 实测通），单聊/查询失败退回会话标签。
    # 标题带群名=任务中心列表一眼认群；描述首行带「群名｜项目·会话」双定位。
    chat_name = os.environ.get("FEISHU_CHAT_NAME", "").strip()
    label = os.environ.get("FEISHU_CHAT_LABEL", "").strip()
    origin_tag = chat_name or label.strip("[]") or "飞书机器人会话"
    full_summary = f"[{origin_tag}] {summary}"
    cfg = _cfg()
    uid = (cfg.get("ALLOWED_USERS", "").split(",")[0] or "").strip()
    # 描述首行自动带来源（11-24 定谳）：origin.refer_resources 第三方写入无效
    # （t100003 实测服务端不采纳），UI「来源」区靠不传 origin 让飞书自动标
    # 「来自 Claude Bot」应用身份；群聊级溯源=标题群名+描述首行双定位。
    desc = (args.get("description") or "").strip()
    head = f"来源：{origin_tag}" + (f"｜{label}" if label and label.strip("[]") != origin_tag else "")
    desc = f"{head}\n\n{desc}" if desc else f"{head}（任务进行中，进展见下方时间线）"
    body = (InputTask.builder()
            .summary(full_summary[:200])
            .description(desc)
            .members([Member.builder().id(uid).type("user").role("assignee").build()] if uid else None)
            .build())
    req = (CreateTaskRequest.builder()
           .request_body(body).user_id_type("open_id").build())
    resp = _client().task.v2.task.create(req)
    if not resp.success():
        return {"error": f"任务创建失败 code={resp.code} msg={resp.msg}"}
    t = resp.data.task
    guid = t.guid or ""
    # 挂「Claude 任务」清单（总览入口）；清单被删等失败不阻塞——任务本体已建成
    if guid:
        try:
            req2 = (AddTasklistTaskRequest.builder()
                    .task_guid(guid)
                    .user_id_type("open_id")
                    .request_body(AddTasklistTaskRequestBody.builder()
                                  .tasklist_guid(TASKLIST_GUID).build())
                    .build())
            _client().task.v2.task.add_tasklist(req2)
        except Exception:
            pass
    rows = _reg_load(chat_id)
    rows.append({"guid": guid, "summary": full_summary,
                 "url": getattr(t, "url", "") or "", "status": "todo",
                 "created": time.time()})
    _reg_save(chat_id, rows)
    _send_text(chat_id, f"📋 已创建任务跟踪：{full_summary}\n"
                        f"进展会自动更新到任务描述（任务中心「Claude 任务」清单可见）")
    return {"task_guid": guid, "summary": full_summary,
            "note": "已指派给用户并在飞书任务中心可见；阶段性进展用 update_task，"
                    "完成时用 complete_task 收尾"}


def tool_update_task(args: dict) -> dict:
    progress = (args.get("progress") or "").strip()
    if not progress:
        return {"error": "progress 不能为空（一句话阶段进展）"}
    guid, summary = _resolve_guid(args)
    if not guid:
        return {"error": summary}
    try:
        t = _get_task(guid)
        desc = (getattr(t, "description", "") or "")
        if not desc.strip():
            desc = "（任务进行中，进展见下方时间线）"
        desc = f"{desc}\n\n[{_now_tag()}] 进展：{progress}"
        _patch_task(guid, ["description"], description=desc)
    except Exception as e:
        return {"error": f"进展更新失败：{e}"}
    return {"ok": True, "task_guid": guid, "summary": summary,
            "note": "进展已追加到任务描述（时间线）"}


def tool_complete_task(args: dict) -> dict:
    guid, summary = _resolve_guid(args)
    if not guid:
        return {"error": summary}
    result = (args.get("result") or "").strip()
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    try:
        t = _get_task(guid)
        desc = (getattr(t, "description", "") or "")
        done_line = f"[{_now_tag()}] ✅ 完成" + (f"：{result}" if result else "")
        desc = f"{desc}\n\n{done_line}"
        _patch_task(guid, ["description", "completed_at"],
                    description=desc, completed_at=int(time.time() * 1000))
    except Exception as e:
        return {"error": f"任务完成失败：{e}"}
    rows = _reg_load(chat_id)
    for r in rows:
        if r.get("guid") == guid:
            r["status"] = "done"
            r["done_at"] = time.time()
    _reg_save(chat_id, rows)
    _send_text(chat_id, f"✅ 任务完成：{summary}" + (f"｜{result[:80]}" if result else ""))
    return {"ok": True, "task_guid": guid}


TOOLS = [
    {"name": "create_task",
     "description": "在飞书任务中心创建任务，用于长任务过程跟踪。适用条件：本任务预计"
                    "需要多轮/多阶段的复杂工作（如调研、排查、分步开发）。创建后用户在"
                    "飞书任务中心「Claude 任务」清单总览可见，你会自动指派用户为负责人。"
                    "简单一两轮就能答完的问题不要建任务。",
     "inputSchema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "任务名，一句话（会自动加会话标签前缀）"},
         "description": {"type": "string", "description": "任务目标说明（可选）"}},
         "required": ["summary"]}},
    {"name": "update_task",
     "description": "给跟踪中的飞书任务追加一条阶段性进展（带时间戳写进任务描述，用户"
                    "点开任务看时间线）。粒度=完成了一个有意义的子阶段（拿到关键结果、"
                    "做完一轮验证等），不是每一步工具调用都更新。不传 task_guid 默认"
                    "更新本会话最近的未完成任务。",
     "inputSchema": {"type": "object", "properties": {
         "progress": {"type": "string", "description": "进展一句话（如「完成基线测量，吞吐提升 12%」）"},
         "task_guid": {"type": "string", "description": "任务 guid（可选，默认本会话最近未完成任务）"}},
         "required": ["progress"]}},
    {"name": "complete_task",
     "description": "长任务全部完成时收尾：任务置完成、结论写进描述、登记表关闭。"
                    "不传 task_guid 默认收尾本会话最近的未完成任务。",
     "inputSchema": {"type": "object", "properties": {
         "result": {"type": "string", "description": "最终结论一句话（可选）"},
         "task_guid": {"type": "string", "description": "任务 guid（可选）"}},
         "required": []}},
]
_HANDLERS = {"create_task": tool_create_task,
             "update_task": tool_update_task,
             "complete_task": tool_complete_task}


def main() -> None:
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
                "serverInfo": {"name": "feishu-task", "version": "1.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = ((req.get("params") or {}).get("name") or "")
            handler = _HANDLERS.get(name)
            if handler is None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": f"unknown tool {name}"}})
                continue
            args = (req.get("params") or {}).get("arguments") or {}
            try:
                out = handler(args)
            except Exception as e:
                out = {"error": f"工具内部异常：{e!r}"}
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False)}]}})
        elif method.startswith("notifications/"):
            pass
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
