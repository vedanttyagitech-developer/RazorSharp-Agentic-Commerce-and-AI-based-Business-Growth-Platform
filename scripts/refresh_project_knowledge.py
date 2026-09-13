"""Refresh only explicitly curated Python evidence; --check detects source drift in CI."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "packages/commerce-api/src/commerce_api/knowledge/project.json"


def refreshed() -> str:
    documents = json.loads(INDEX.read_text())
    for doc in documents:
        path = (ROOT / doc["source"]).resolve()
        if not path.is_relative_to(ROOT / "packages") or path.suffix != ".py":
            raise ValueError("Evidence must be an explicitly selected package Python source")
        source = path.read_text()
        module = ast.parse(source)
        symbol = doc["source_symbol"]
        if symbol:
            nodes = [
                n
                for n in ast.walk(module)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == symbol
            ]
            if len(nodes) != 1:
                raise ValueError(f"Unresolved evidence symbol: {symbol}")
            node = nodes[0]
            excerpt = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            line = node.lineno
        else:
            excerpt = ast.get_docstring(module) or ""
            line = 1
        doc["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        doc["source_line"] = str(line)
        doc["evidence_excerpt"] = excerpt[:4500]
        doc["evidence_scope"] = "selected excerpt, not the complete implementation"
    return json.dumps(documents, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = refreshed()
    if args.check:
        if INDEX.read_text() != content:
            raise SystemExit("Project knowledge evidence drift: refresh and review source excerpts")
    else:
        INDEX.write_text(content)
