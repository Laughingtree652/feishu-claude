#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目自检：语法、目录布局约定、核心纯函数行为。零副作用——
不 import bot/notify（它们的模块级代码会连 lark、搬状态文件），
只对源码做 ast 解析，并提取纯函数在受控命名空间里跑用例。

用法：  python tests/selfcheck.py   （在项目任意位置均可，路径自定位）
退出码：0=全部通过；1=有失败（输出里标 FAIL）。
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["bot.py", "notify.py", "perm_mcp.py", "perm_gate.py"]

failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


# ---------------- 1. 语法 ----------------
sources = {}
for fname in SOURCES:
    try:
        src = (ROOT / fname).read_text(encoding="utf-8")
        ast.parse(src)
        sources[fname] = src
        check(f"语法 {fname}", True)
    except (OSError, SyntaxError) as e:
        check(f"语法 {fname}", False, repr(e))

bot_src = sources.get("bot.py", "")
notify_src = sources.get("notify.py", "")
gate_src = sources.get("perm_gate.py", "")

# ---------------- 2. 目录布局约定（静态断言） ----------------
# bot.py：状态文件一律走 STATE_DIR、日志走 LOGS_DIR；BASE_DIR 只许引用
# .env / perm_mcp.py / 目录定义 / 迁移循环里的变量形式
bad = [ln.strip() for ln in bot_src.splitlines()
       if 'BASE_DIR / "' in ln and not any(
           k in ln for k in ('".env"', '"perm_mcp.py"', '"data"', '"logs"', 'BASE_DIR / name'))]
check("bot.py 无根目录状态文件残留", not bad, "; ".join(bad[:3]))

# bot.py：所有 STATE_DIR 落点必须登记进 _MIGRATE_FILES（新增状态文件别忘迁移）
tree = ast.parse(bot_src)
state_files, migrate_list = set(), set()
for node in ast.walk(tree):
    # STATE_DIR / "xxx.json"
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name) and node.left.id == "STATE_DIR"
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        state_files.add(node.right.value)
migrate_node = next((n for n in tree.body if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", "") == "_MIGRATE_FILES" for t in n.targets)), None)
if migrate_node is not None:
    migrate_list = {e.value for e in migrate_node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
missing = state_files - migrate_list
check(f"STATE_DIR 文件全部登记迁移（{len(state_files)} 个落点）", not missing, f"漏登记: {sorted(missing)}")

# notify.py / perm_gate.py：状态指向 data/、日志指向 logs/
check("notify.py 状态/日志指向 data/、logs/",
      'STATE = BASE / "data"' in notify_src and 'BASE / "logs" / "notify.log"' in notify_src)
check("perm_gate.py 状态/日志指向 data/、logs/",
      '"data" / "mute.flag"' in gate_src and '"logs" / "notify.log"' in gate_src)

# .gitignore 覆盖新旧布局
gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
check(".gitignore 覆盖 data/ 与 logs/", "data/" in gi and "logs/" in gi)

# ---------------- 3. 纯函数行为（ast 提取，exec 到受控命名空间） ----------------
WANTED_FUNCS = {"_is_table_sep", "_md_tables_need_blank", "_split_chunks"}
WANTED_ASSIGNS = {"MAX_CHUNK"}
ns = {"os": os, "sys": sys}
got = set()
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<extract>", "exec"), ns)
        got.add(node.name)
    elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in WANTED_ASSIGNS for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<extract>", "exec"), ns)
check(f"纯函数提取成功（{sorted(got)}）", got == WANTED_FUNCS)

if got == WANTED_FUNCS:
    f_sep, f_tbl, f_chunk = ns["_is_table_sep"], ns["_md_tables_need_blank"], ns["_split_chunks"]

    # _is_table_sep：分隔行判定（字符集只含 | - : 空格）
    for line, want in [("| --- |", True), ("|:--:|", True), ("|---|", True),
                       ("| a | b |", False), ("---", False), ("", False),
                       ("| --- | x", False)]:
        check(f"_is_table_sep({line!r})=={want}", f_sep(line) == want)

    # _md_tables_need_blank：表头前补空行；已合规不动（幂等）
    check("表头紧贴段落 → 补空行",
          f_tbl("前文\n| a | b |\n| --- | --- |\n| 1 | 2 |")
          == "前文\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    check("已有空行 → 原样（幂等）",
          f_tbl("前文\n\n| a |\n| --- |\n| 1 |") == "前文\n\n| a |\n| --- |\n| 1 |")
    check("开头就是表格 → 不动",
          f_tbl("| a |\n| --- |\n| 1 |") == "| a |\n| --- |\n| 1 |")

    # _split_chunks：表格原子块 + 内容不丢
    table = "\n".join(["| 阶段 | 事项 | 结果 |", "| --- | --- | --- |"]
                      + [f"| 段{i} | 事项{i} | 结果{i} |" for i in range(6)])
    text = f"先说明：\n\n{table}\n\n收尾一句话。"
    whole = f_chunk(text, cap=len(text) + 10)
    check("小表整段不拆（cap 富余）", len(whole) == 1 and whole[0] == text.strip("\n"))
    tight = f_chunk(text, cap=80)
    check("cap 80 内容不丢（行集合包含）",
          all(any(ln in seg for seg in tight) for ln in text.splitlines() if ln.strip()))
    atomic = f_chunk(table, cap=400)
    check("表头与分隔行不被切开", any("| 阶段 |" in seg and "| --- |" in seg for seg in atomic))

# ---------------- 汇总 ----------------
print(f"\n{'=' * 46}\n{'全部通过' if not failures else '失败 ' + str(len(failures)) + ' 项: ' + '; '.join(failures)}")
sys.exit(1 if failures else 0)
