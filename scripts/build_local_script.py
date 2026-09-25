#!/usr/bin/env python3
"""Embed the canonical, stdlib-only transcript contract in the standalone client."""
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
