"""Watchlists stay in platform user storage across bundle upgrades."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from webui.services import paths


@pytest.mark.parametrize("platform,os_name", [("darwin", "posix"), ("win32", "nt"), ("linux", "posix")])
def test_platform_user_directory_is_stable_across_installs(tmp_path, monkeypatch, platform, os_name):
    home = tmp_path / "home"
    environment = {"APPDATA": str(tmp_path / "roaming"), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(paths, "os", SimpleNamespace(name=os_name, environ=environment))
    monkeypatch.setattr(Path, "home", lambda: home)
    expected_base = (home / "Library/Application Support" if platform == "darwin" else
                     tmp_path / "roaming" if os_name == "nt" else tmp_path / "xdg")
    environment["KRONOS_PROJECT_ROOT"] = str(tmp_path / "old-install")
    first = paths.user_root()
    environment["KRONOS_PROJECT_ROOT"] = str(tmp_path / "new-install")
    assert paths.user_root() == first == expected_base / "com.lumo.trade"
    assert expected_base / "com.kronos.app/config/watchlist.json" in paths.legacy_watchlist_paths(first)
    assert expected_base / "Kronos/config/watchlist.json" in paths.legacy_watchlist_paths(first)
    assert all("install" not in str(path) for path in paths.legacy_watchlist_paths(first))


def test_custom_user_directory_does_not_import_other_users_watchlists(tmp_path, monkeypatch):
    custom = tmp_path / "isolated-user"
    monkeypatch.setenv("KRONOS_USER_DIR", str(custom))
    assert paths.user_root() == custom
    assert paths.legacy_watchlist_paths(custom) == []
