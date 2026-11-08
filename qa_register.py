# Stop hook：任何 Claude 会话（桌面窗口/飞书分身）每回合结束，向问答档案记一行。
# 背景：11-08 用户痛点——终端会话里问过的问题无法记录、无法快速定位重读。
# 数据流：本 hook（每回合 append）+ backfill_qa.py（一次性回填历史）→ data/qa/<会话ID>.jsonl
# → 飞书 bot /qa 查询与重读（只对聊天绑定的会话生效）。
# 设计：同 roster_register 的性格——append-only、静默容错、绝不报错、绝不阻塞回合结束。
import sys, json, os
from pathlib import Path

QA_DIR = Path(__file__).resolve().parent / "data" / "qa"
TAIL_WINDOW = 2 * 1024 * 1024   # 尾扫窗口：本回合必在转录尾部 2MB 内


def _user_text(d) -> str:
    """真人用户文本；tool_result 形态/isMeta/压缩摘要/合成注入/slash 一律返回空。
    过滤口径与 bot._session_preview 一致（11-06 修过的形态学）。"""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return ""
    m = d.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        t = c.strip()
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        t = " ".join(x for x in parts if x).strip()
    else:
        return ""
    if not t or t.startswith("/") or t.startswith("<"):
        return ""
    return t


def _as_text(d) -> str:
    """assistant 行的回答文本（排除 thinking 块），多 text 块拼接。"""
    if d.get("type") != "assistant":
        return ""
    m = d.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return "\n\n".join(b.get("text", "") for b in c
                           if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def scan_turns(lines) -> list:
    """转产行 → 回合列表。回合 = 一条真人 user 到下一条真人 user 之间；
    a = 该区间全部 assistant 回答拼接（hook/backfill 取 [:200] 做预览，
    bot /qa 重读时取全文）。供本 hook 与 backfill_qa.py 共用。"""
    turns, cur = [], None
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        q = _user_text(d)
        if q:
            if cur and cur["q_uuid"]:
                turns.append(cur)
            cur = {"ts": str(d.get("timestamp") or ""), "q_uuid": str(d.get("uuid") or ""),
                   "q": q, "parts": []}
            continue
        if cur is not None:
            t = _as_text(d)
            if t:
                cur["parts"].append(t)
    if cur and cur["q_uuid"]:
        turns.append(cur)
    for t in turns:
        t["a"] = "\n\n".join(t.pop("parts"))
    return turns


def main() -> None:
    try:
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        return
    sid = str(d.get("session_id") or "")
    tp = str(d.get("transcript_path") or "")
    if not sid or not tp:
        return
    try:
        f = Path(tp)
        if not f.exists():
            return
        with f.open("rb") as fh:
            size = f.stat().st_size
            fh.seek(max(0, size - TAIL_WINDOW))
            if fh.tell():
                fh.readline()          # 从窗口起点先跳过半行
            data = fh.read().decode("utf-8", "replace")
        turns = scan_turns(data.splitlines())
        if not turns:
            return
        t = turns[-1]                   # 本回合（Stop 时转录尾部就是刚结束的回合）
    except Exception:
        return
    rec = {"ts": t["ts"], "sid": sid, "tp": tp, "q_uuid": t["q_uuid"],
           "q": t["q"][:120], "pv": t["a"][:200],
           "cwd": str(d.get("cwd") or ""),
           "bot_child": os.environ.get("FEISHU_BOT_CHILD", "") == "1"}
    # 同回合多 Stop（权限中断后继续等）→ 文件尾同 q_uuid 去重
    try:
        out = QA_DIR / f"{sid}.jsonl"
        if out.exists():
            for ln in reversed(out.read_text(encoding="utf-8").splitlines()[-3:]):
                try:
                    if json.loads(ln).get("q_uuid") == rec["q_uuid"]:
                        return
                except Exception:
                    pass
        QA_DIR.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
