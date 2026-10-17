"""Migrations must load a database URL whose password contains "%" (URL-encoded)."""
import importlib.util
import pathlib
import sys
import types

import pytest
from alembic.config import Config

ENV_PY = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "env.py"
URL = "postgresql+asyncpg://cara:p%40ss%25word@db.example.test:5432/cara"


def _load_env_with(monkeypatch, url):
    config = Config()
    seen = {}

    fake_context = types.SimpleNamespace(
        config=config,
        is_offline_mode=lambda: True,
        configure=lambda **kwargs: seen.update(kwargs),
        begin_transaction=lambda: _NullTx(),
        run_migrations=lambda: None,
    )
    import alembic

    monkeypatch.setattr(alembic, "context", fake_context)
    from app.core import config as app_config

    monkeypatch.setattr(type(app_config.settings), "DATABASE_URL_ASYNC", property(lambda self: url))
    spec = importlib.util.spec_from_file_location("cara_migrations_env_under_test", ENV_PY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return config, seen


class _NullTx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_percent_in_password_does_not_break_migrations(monkeypatch):
    config, seen = _load_env_with(monkeypatch, URL)
    assert config.get_main_option("sqlalchemy.url") == URL
    assert seen["url"] == URL


def test_unescaped_configparser_would_have_failed():
    # Documents the original failure this guards against.
    with pytest.raises(ValueError):
        Config().set_main_option("sqlalchemy.url", URL)
