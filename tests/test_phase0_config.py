from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from island_quant.config import AppSettings, TradingMode, load_settings


def test_default_configuration_is_safe() -> None:
    settings = load_settings("config/default.yaml")

    assert settings.trading.mode is TradingMode.BACKTEST
    assert settings.trading.live_enabled is False
    assert settings.trading.allow_short is False
    assert settings.trading.initial_cash == Decimal("1000000")


def test_environment_override_is_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ISLAND_QUANT__TRADING__INITIAL_CASH", "2500000")

    settings = load_settings("config/default.yaml")

    assert settings.trading.initial_cash == Decimal("2500000")


def test_live_mode_fails_without_both_explicit_gates() -> None:
    with pytest.raises(ValidationError, match="live mode requires"):
        AppSettings.model_validate({"environment": "live", "trading": {"mode": "live"}})


def test_unknown_configuration_key_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text(yaml.safe_dump({"unexpected": True}), encoding="utf-8")

    with pytest.raises(ValidationError, match="unexpected"):
        load_settings(config_path)
