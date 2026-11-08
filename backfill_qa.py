# 一次性回填：扫 ~/.claude/projects 下全部历史转录，把每个会话的每回合
# （真人问题 + 回答预览 + 定位锚）写进 data/qa/<会话ID>.jsonl。11-08 用户要求
# 「想看历史问题」——hook 只向前看，历史靠这一趟补齐。
# 幂等：已存在的 <sid>.jsonl 跳过（怕重复跑出双份）；--merge 可对已有文件
# 补增量（按 q_uuid 去重合并）。用法：
#   .venv/Scripts/python.exe backfill_qa.py            # 全量回填（已有跳过）
#   .venv/Scripts/python.exe backfill_qa.py --merge   # 已有文件补增量
import json
import sys
from pathlib import Path
from qa_register import QA_DIR, scan_turns


def _existing_uuids(out: Path) -> set:
    """已记录的 q_uuid 集合（增量合并去重用）。"""
    seen = set()
    try:
        for ln in out.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(ln).get("q_uuid") or "")
            except Exception:
                pass
    except OSError:
        pass
    return seen


def backfill(merge: bool = False) -> None:
    projects = Path.home() / ".claude" / "projects"
    files = sorted(projects.glob("*/*.jsonl"))
    done = skip = turns_total = 0
    for tp in files:
        sid = tp.stem
        if sid.count("-") != 4:        # 非会话转录（36 位 uuid 五段命名才算）
            continue
        out = QA_DIR / f"{sid}.jsonl"
        if out.exists() and not merge:
            skip += 1
            continue
        seen = _existing_uuids(out) if out.exists() else set()
        turns = [t for t in scan_turns(_iter_lines(tp))
                 if t["q_uuid"] and t["q_uuid"] not in seen]
        if not turns:
            if not out.exists():
                skip += 1               # 没有可记回合（纯工具/无真人输入的会话）
            continue
        QA_DIR.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            for t in turns:
                fh.write(json.dumps({"ts": t["ts"], "sid": sid, "tp": str(tp),
                                     "q_uuid": t["q_uuid"], "q": t["q"][:120],
                                     "pv": t["a"][:200], "cwd": "", "bot_child": False},
                                    ensure_ascii=False) + "\n")
        done += 1
        turns_total += len(turns)
        print(f"  回填 {sid[:8]} ← {tp.parent.name}: {len(turns)} 回合")
    print(f"完成：回填会话 {done} 个 / 共 {turns_total} 回合 / 跳过 {skip} 个")


def _iter_lines(tp: Path):
    """流式逐行（几十 MB 转录不整载内存）。"""
    with tp.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            yield ln


if __name__ == "__main__":
    backfill(merge="--merge" in sys.argv)
