"""Tests for custom-strategy authoring helpers (api/main._validate_strategy_source,
_safe_custom_filename) used by the upload / save / validate endpoints."""
import pytest

from fastapi import HTTPException
from api.main import _validate_strategy_source, _safe_custom_filename

_VALID = b"""
from strategies.base import BaseStrategy

class MyEdgeStrategy(BaseStrategy):
    async def on_ticker(self, event):
        return []
    def get_status(self):
        return {}
"""


def test_validate_returns_strategy_ids():
    ids = _validate_strategy_source(_VALID)
    assert ids == ["my_edge_strategy"]            # class name → snake_case id


def test_validate_rejects_syntax_error():
    with pytest.raises(HTTPException) as e:
        _validate_strategy_source(b"def oops(:\n    pass\n")
    assert e.value.status_code == 400


def test_validate_rejects_no_base_strategy():
    with pytest.raises(HTTPException) as e:
        _validate_strategy_source(b"x = 1\n")
    assert e.value.status_code == 400


def test_validate_ignores_imported_base_classes():
    # Only classes DEFINED in this source count, not the imported BaseStrategy itself.
    ids = _validate_strategy_source(_VALID)
    assert "base_strategy" not in ids


def test_safe_filename_appends_py_and_rejects_traversal():
    assert _safe_custom_filename("foo") == "foo.py"
    assert _safe_custom_filename("foo.py") == "foo.py"
    for bad in ("../foo.py", "a/b.py", "_private.py", ""):
        with pytest.raises(HTTPException):
            _safe_custom_filename(bad)
