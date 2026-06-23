"""
Custom strategy loader.
Scans strategies/custom/ for .py files, imports them, and returns all
BaseStrategy subclasses found inside.
"""
from __future__ import annotations
import importlib.util
import inspect
import re
import sys
from pathlib import Path

from strategies.base import BaseStrategy

CUSTOM_DIR = Path(__file__).parent / "custom"


def _class_to_id(class_name: str) -> str:
    """MyCustomStrategy → my_custom_strategy"""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s).lower()


def scan() -> tuple[dict[str, tuple[type, str]], dict[str, str]]:
    """
    Scan custom/ directory and return discovered strategies.

    Returns:
        found:  {strategy_id: (StrategyClass, source_file_path)}
        errors: {filename: error_message}
    """
    CUSTOM_DIR.mkdir(exist_ok=True)
    found: dict[str, tuple[type, str]] = {}
    errors: dict[str, str] = {}

    for py_file in sorted(CUSTOM_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"strategies.custom.{py_file.stem}"
        try:
            # Drop cached version so hot-reload works
            sys.modules.pop(module_name, None)
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)

            for name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, BaseStrategy)
                    and obj is not BaseStrategy
                    and obj.__module__ == module_name
                ):
                    sid = _class_to_id(name)
                    found[sid] = (obj, str(py_file))
        except Exception as exc:
            errors[py_file.name] = str(exc)

    return found, errors
