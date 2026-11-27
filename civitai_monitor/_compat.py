"""Module-attribute resolution helpers preserving monkeypatch seams."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


def resolve_mod(mod: ModuleType | None, module_name: str) -> ModuleType | None:
    """Return explicit ``mod`` or look up ``sys.modules[module_name]`` at call time."""
    if mod is not None:
        return mod
    return sys.modules.get(module_name)


def mod_attr(mod: ModuleType | None, module_name: str, name: str, default: Any) -> Any:
    """Resolve ``name`` on ``mod`` (or ``sys.modules[module_name]``), falling back to ``default``."""
    target = resolve_mod(mod, module_name)
    if target is not None:
        return getattr(target, name, default)
    return default
