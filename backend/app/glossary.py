"""Loads a technical glossary (YAML) and renders it as a prompt context block."""
from __future__ import annotations

from pathlib import Path

import yaml

# backend/app/glossary.py -> repo root (mirrors /srv in the Docker image,
# where config/ is a sibling of backend/). Resolving here instead of against
# cwd means "config/foo.yaml" works the same whether the app was started
# from the repo root, from backend/, or inside the container.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_glossary_block(path: str | None) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _PROJECT_ROOT / file_path
    if not file_path.is_file():
        return ""
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    terms = data.get("terms", [])
    lines = []
    for term in terms:
        if isinstance(term, str):
            lines.append(f"- {term}")
        else:
            name = term.get("term", "")
            note = term.get("note", "")
            lines.append(f"- {name}" + (f" ({note})" if note else ""))
    return "\n".join(lines)
