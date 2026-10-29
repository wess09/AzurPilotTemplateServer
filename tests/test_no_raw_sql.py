"""Protect the maintainability requirement across application code and migrations."""

import ast
from pathlib import Path


def test_no_raw_sql_entry_points():
    for directory in ("templateserver", "migrations", "tools"):
        for path in Path(directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
                assert name not in {"text", "exec_driver_sql"}, f"Raw SQL entry point in {path}:{node.lineno}"
                if name in {"execute", "executemany", "CheckConstraint"} and node.args:
                    assert not isinstance(node.args[0], (ast.Constant, ast.JoinedStr)), (
                        f"SQL string in {path}:{node.lineno}"
                    )
