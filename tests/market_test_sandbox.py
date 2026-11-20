"""Isolate the offline acceptance suite before importing any application routes."""
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextmanager
def offline_market_runtime():
    with TemporaryDirectory(prefix="kronos-market-") as tmp, patch.dict(os.environ):
        root = Path(tmp)
        for key in list(os.environ):
            if key.startswith("KRONOS_") or key == "TUSHARE_TOKEN":
                del os.environ[key]
        for kind in ("CONFIG", "DATA", "RESULTS", "LOGS", "MODELS"):
            os.environ[f"KRONOS_{kind}_DIR"] = str(root / kind.lower())
        config = root / "config"
        config.mkdir()
        os.environ.update(KRONOS_USER_DIR=tmp, KRONOS_SQLITE_PATH=str(root / "test.sqlite"),
                          KRONOS_SOURCE_CONFIG_DIR=str(config), KRONOS_DISABLE_TORCH="1",
                          MPLCONFIGDIR=str(root / "matplotlib"))
        # Even an empty test config normally falls back to the developer's source token.
        with (patch("socket.getaddrinfo", side_effect=AssertionError("Offline check: no DNS")),
              patch("socket.socket.connect", side_effect=AssertionError("Offline check: no network")),
              patch("webui.services.configuration_service.RuntimeConfigurationService._candidate_paths",
                    lambda self, name: [self.user_config_dir / name]),
              patch("data_store.tushare_client._load_token", return_value="")):
            yield root
