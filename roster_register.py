# SessionStart hook：任何 Claude 窗口/飞书分身一上线就向 bot 花名册登记一行。
# 背景：多 agent 协作里 session_id 是唯一永不变的身份锚（ref 换代即失效），
# 花名册 data/roster.jsonl 由 bot /roster 命令渲染给飞书侧随时查阅。
# 设计：append-only + 尾部 60 秒去重（分身每条消息续接都算一次 SessionStart）
# + 超限截尾。全链路静默容错——hook 永不报错、绝不阻塞会话启动。
import sys, json, os, time
from pathlib import Path

ROSTER = Path(__file__).resolve().parent / "data" / "roster.jsonl"
DEDUP_WINDOW = 60          # 同会话 60 秒内重复登记跳过（分身高频续接）
TRIM_SIZE = 262144         # 超过 256KB 截尾，保留最新 400 行
TRIM_KEEP = 400


def main() -> None:
    try:
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        d = {}
    sid = str(d.get("session_id") or "")
    if not sid:
        return
    ts = time.time()
    try:
        lines = ROSTER.read_text(encoding="utf-8").splitlines() if ROSTER.exists() else []
    except OSError:
        lines = []
    # 尾部去重：同 session_id 刚登记过就跳过（分身每条消息都触发 SessionStart）
    for ln in reversed(lines[-5:]):
        try:
            r = json.loads(ln)
            if r.get("sid") == sid and ts - float(r.get("ts") or 0) < DEDUP_WINDOW:
                return
        except Exception:
            pass
    rec = {"ts": round(ts, 3), "sid": sid,
           "cwd": str(d.get("cwd") or ""),
           "tp": str(d.get("transcript_path") or ""),
           "bot_child": os.environ.get("FEISHU_BOT_CHILD", "") == "1",
           "source": str(d.get("source") or "")}
    try:
        ROSTER.parent.mkdir(parents=True, exist_ok=True)
        with ROSTER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if ROSTER.stat().st_size > TRIM_SIZE:
            keep = lines[-TRIM_KEEP:] + [json.dumps(rec, ensure_ascii=False)]
            ROSTER.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    main()
