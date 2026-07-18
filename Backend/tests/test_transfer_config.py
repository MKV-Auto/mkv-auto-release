import os
from pathlib import Path

from core import transfer_config


def test_transfer_config_round_trip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "transfer_config.json"
    monkeypatch.setattr(transfer_config, "_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(transfer_config._config, "_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(transfer_config._config, "get_mkvauto_data", lambda: tmp_path / "data")

    saved = transfer_config.save_transfer_config("local", transfer_dir=str(tmp_path / "dest"), output_dir=str(tmp_path / "out"))
    assert saved["mode"] == "local"
    assert saved["output_dir"] == str(tmp_path / "out")

    loaded = transfer_config.load_transfer_config()
    assert loaded["mode"] == "local"
    assert loaded["transfer_dir"] == str(tmp_path / "dest")
    assert loaded["output_dir"] == str(tmp_path / "out")


def test_transfer_config_env_default(tmp_path, monkeypatch):
    cfg_file = tmp_path / "transfer_config.json"
    monkeypatch.setattr(transfer_config, "_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(transfer_config._config, "_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(transfer_config._config, "get_mkvauto_data", lambda: tmp_path / "data")

    env_dir = tmp_path / "env_dir"
    monkeypatch.setenv("MAKEMKV_TRANSFER_DIR", str(env_dir))

    cfg = transfer_config.load_transfer_config()
    assert Path(cfg["transfer_dir"]).resolve() == env_dir.resolve()
