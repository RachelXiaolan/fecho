#!/usr/bin/env python3
"""把 fecho/scan_contract.py 嵌进本机脚本 presets/local/fecho_local.py。

本机脚本是单文件分发的，不能 import 包里的模块，只能把共用规则原样抄进去。
改了 scan_contract.py 之后跑一次：python3 scripts/build_local_script.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "fecho" / "scan_contract.py"
CLIENT = ROOT / "fecho" / "presets" / "local" / "fecho_local.py"
START = "# BEGIN GENERATED SCAN CONTRACT"
END = "# END GENERATED SCAN CONTRACT"

source = CLIENT.read_text(encoding="utf-8")
contract = CONTRACT.read_text(encoding="utf-8").rstrip()
start = source.index(START) + len(START)
end = source.index(END, start)
source = source[:start] + "\n\n" + contract + "\n\n" + source[end:]
CLIENT.write_text(source, encoding="utf-8")
