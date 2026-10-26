# -*- coding: utf-8 -*-
"""语法自检（跑完可删）。"""
import ast
from pathlib import Path

for f in ["bot.py", "notify.py"]:
    src = Path(f).read_text(encoding="utf-8")
    ast.parse(src)
    print(f, "syntax OK")
