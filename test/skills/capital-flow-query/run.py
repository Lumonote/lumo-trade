#!/usr/bin/env python3
"""Runtime entry for the Kronos data-query skill.

Thin wrapper around the shared dispatcher: it pre-sets the skill name so the
invocation is simply ``python run.py <query|list> --function <fn> ...``.
Run ``python run.py list`` or see SKILL.md for the function catalogue.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling _shared package importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.cli import main  # noqa: E402

SKILL_NAME = "capital-flow-query"

if __name__ == "__main__":
    argv = sys.argv[1:]
    # ``list`` here lists this skill's own functions; keep it but the shared
    # global list is available via the top-level README / manifest.
    raise SystemExit(main(argv, skill_default=SKILL_NAME))
