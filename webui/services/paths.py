"""Path helpers for source and packaged WebUI runtimes."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    configured = os.environ.get("KRONOS_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]


def user_root(app_name: str = "com.lumo.trade") -> Path:
    configured = os.environ.get("KRONOS_USER_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / app_name
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / app_name
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / app_name
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_watchlist_paths(root: Path) -> list[Path]:
    """Known pre-rename stores; never import personal data from the app bundle.

    Explicit/custom user directories remain isolated. The service migrates only
    when the destination is absent, so an intentionally empty list stays empty.
    """
    root = Path(root)
    if root.name != "com.lumo.trade":
        return []
    roots = [root.parent / name for name in ("com.kronos.app", "com.lumo.app", "Kronos")]
    if sys.platform == "darwin" and root.parent == Path.home() / "Library" / "Application Support":
        roots.extend(Path.home() / "Documents" / name for name in ("Kronos", "Lumo"))
    return [candidate / "config" / "watchlist.json" for candidate in roots]


def results_dir() -> Path:
    """Single source of truth for the opportunity/report output directory.

    Honors KRONOS_RESULTS_DIR when set (packaging / power users), otherwise
    falls back to ``user_root()/results``. Both the WebUI reader
    (``webui/core.py``) and the CLI writer (``scripts/run_opportunity_discovery.py``)
    resolve through here so desktop / dev / command-line read & write the same place.
    """
    configured = os.environ.get("KRONOS_RESULTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return user_root() / "results"


def scoring_config_path() -> Path:
    """Single source of truth for ``scoring_runtime_config.json`` (read & write).

    Resolution: ``KRONOS_SCORING_CONFIG`` env > ``user_root()/config/...``
    (seeded once from the repo's ``config/`` copy when absent) > the user
    path even if nothing exists yet (it is the canonical write target).

    Both the scorer (``analysis/opportunity_scorer.py``) and the auto
    optimizer (``scripts/auto_backtest.py``) resolve through here, so the
    packaged desktop app and quick_start CLI mutate/consume the SAME config —
    previously each side kept its own drifting copy, which is why the two
    sides scored the same stocks differently.
    """
    configured = os.environ.get("KRONOS_SCORING_CONFIG")
    if configured:
        return Path(configured).expanduser()
    user_cfg = user_root() / "config" / "scoring_runtime_config.json"
    if not user_cfg.exists():
        repo_cfg = project_root() / "config" / "scoring_runtime_config.json"
        if repo_cfg.exists():
            user_cfg.parent.mkdir(parents=True, exist_ok=True)
            user_cfg.write_text(repo_cfg.read_text(encoding="utf-8"), encoding="utf-8")
    return user_cfg


def ensure_user_subdirs(root: Path) -> None:
    for name in ("data", "logs", "models", "results", "reports", "integrated_results", "config"):
        (root / name).mkdir(parents=True, exist_ok=True)
