"""Install the Fecho MCP and behavioral Skill into supported local agents."""
import json
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def host_specs(mcp_command: str, home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    home = Path(home or Path.home())
    return {
        "codex": {
            "binary": "codex",
            "probe": ["codex", "mcp", "get", "fecho", "--json"],
            "add": ["codex", "mcp", "add", "fecho", "--", mcp_command],
            "skill": home / ".codex" / "skills" / "fecho",
        },
        "claude-code": {
            "binary": "claude",
            "probe": ["claude", "mcp", "get", "fecho"],
            "add": ["claude", "mcp", "add", "--scope", "user", "fecho", "--", mcp_command],
            "skill": home / ".claude" / "skills" / "fecho",
        },
        "hermes": {
            "binary": "hermes",
            "probe": ["hermes", "mcp", "list"],
            "add": ["hermes", "mcp", "add", "fecho", "--command", mcp_command],
            "skill": home / ".hermes" / "skills" / "fecho",
        },
    }


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _default_mcp_command() -> str:
    beside_python = Path(sys.executable).parent / "fecho-mcp"
    found = shutil.which("fecho-mcp")
    if beside_python.exists():
        return str(beside_python)
    if found:
        return str(Path(found).resolve())
    raise RuntimeError("找不到 fecho-mcp；请先完成 Fecho 安装")


def _skill_text() -> str:
    return resources.files("fecho").joinpath("presets", "skill", "SKILL.md").read_text(
        encoding="utf-8")


def _registration_status(host: str, output: str, expected: str) -> str:
    if host == "codex":
        try:
            parsed = json.loads(output)
            command = parsed.get("command") or (parsed.get("transport") or {}).get("command")
            return "existing" if command == expected else "conflict"
        except (ValueError, AttributeError):
            pass
    if expected in output:
        return "existing"
    if host == "hermes" and "fecho" not in output.lower():
        return "missing"
    return "conflict"


def install_all(
    *,
    home: Optional[Path] = None,
    mcp_command: Optional[str] = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Dict[str, Dict[str, Any]]:
    home = Path(home or Path.home())
    mcp_command = mcp_command or _default_mcp_command()
    specs = host_specs(mcp_command, home)
    skill_text = _skill_text()
    results: Dict[str, Dict[str, Any]] = {}

    for host, spec in specs.items():
        if not which(spec["binary"]):
            results[host] = {"mcp": "not-installed", "skill": None}
            continue

        skill_dir = spec["skill"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_text, encoding="utf-8")

        probe = runner(spec["probe"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        probe_code = int(getattr(probe, "returncode", probe if isinstance(probe, int) else 1))
        if probe_code == 0:
            registration = _registration_status(host, _text(getattr(probe, "stdout", "")), mcp_command)
        else:
            registration = "missing"

        if registration == "missing":
            added = runner(spec["add"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            code = int(getattr(added, "returncode", added if isinstance(added, int) else 1))
            registration = "installed" if code == 0 else "failed"
            error = _text(getattr(added, "stderr", "")).strip() if code else None
        else:
            error = None

        results[host] = {"mcp": registration, "skill": str(skill_dir)}
        if error:
            results[host]["error"] = error
    return results
