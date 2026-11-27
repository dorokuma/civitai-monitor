"""Subprocess CLI entrypoint and facade symbol-parity smoke tests."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _collect_top_level_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    syms: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                syms.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                syms.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    syms.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            syms.add(node.target.id)
    return syms


def test_bot_package_attrs_exist_on_facade() -> None:
    bot_syms = _collect_top_level_symbols(REPO_ROOT / "civitai-bot.py")
    missing: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "civitai_monitor").glob("bot_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "bot"
                and node.attr not in bot_syms
            ):
                missing.append((path.name, node.lineno, node.attr))
    assert not missing, f"bot.<attr> missing on civitai-bot.py: {missing}"


def test_monitor_mod_attrs_exist_on_facade() -> None:
    mon_syms = _collect_top_level_symbols(REPO_ROOT / "monitor.py")
    missing: list[tuple[str, int, object]] = []
    for path in sorted((REPO_ROOT / "civitai_monitor").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "mod_attr"
                and len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant)
                and node.args[2].value not in mon_syms
            ):
                missing.append((path.name, node.lineno, node.args[2].value))
    assert not missing, f"mod_attr target missing on monitor.py: {missing}"


def test_monitor_cli_help_from_outside_cwd(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "monitor.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "monitor" in proc.stdout.lower() or "usage" in proc.stdout.lower()


def test_civitai_bot_import_from_outside_cwd(tmp_path: Path) -> None:
    script = (
        "import importlib.util, sys; "
        f"sys.path.insert(0, {str(REPO_ROOT)!r}); "
        f"spec = importlib.util.spec_from_file_location('civitai_bot_smoke', {str(REPO_ROOT / 'civitai-bot.py')!r}); "
        "mod = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(mod); "
        "assert hasattr(mod, 'main') and hasattr(mod, '_atomic_write') and hasattr(mod, '_cron_failure_headline')"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
