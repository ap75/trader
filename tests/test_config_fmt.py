from decimal import Decimal as D

import pytest

from optbot import fmt
from optbot.bot import check_config
from optbot.config import Settings
from tests.helpers import with_cfg


# ---------- config ----------
def test_settings_from_env(cfg):
    assert cfg.env == "demo" and not cfg.is_mainnet
    assert cfg.base_coins == ["BTC", "ETH", "SOL"]
    assert cfg.tg_trader_ids == {10} and cfg.tg_viewer_ids == {20}
    assert cfg.tg_allowed_chat_ids == {-100111, -100222}
    assert cfg.max_order_qty["BTC"] == D("1")
    assert cfg.sensitive_commands_dm_only is True


@pytest.mark.parametrize("env", ["demo1", "live", "prod", ""])
def test_unknown_env_rejected(cfg, env):
    # клиент идёт в mainnet при любом значении, кроме testnet/demo — опечатка должна ронять запуск
    with pytest.raises(ValueError, match="BYBIT_ENV"):
        with_cfg(cfg, env=env)


def test_mainnet_flag(cfg):
    assert with_cfg(cfg, env="mainnet").is_mainnet


def test_check_config_ok(cfg):
    assert check_config(cfg) == []


def test_check_config_errors(cfg):
    errs = check_config(with_cfg(cfg, tg_token="", tg_trader_ids=set(), tg_viewer_ids=set(),
                                 tg_allowed_chat_ids={123}))
    joined = " ".join(errs)
    assert "TELEGRAM_BOT_TOKEN" in joined and "Никому нет доступа" in joined and "отрицательные" in joined


def test_check_config_service_ids(cfg):
    assert any("служебные" in e for e in check_config(with_cfg(cfg, tg_trader_ids={777000})))


def test_allow_all_view_counts_as_access(cfg):
    assert check_config(with_cfg(cfg, tg_trader_ids=set(), tg_viewer_ids=set(),
                                 allow_all_group_members_view=True)) == []


# ---------- fmt ----------
@pytest.mark.parametrize("x,out", [
    ("0", "-"), ("1090", "1090"), ("12.5", "12.50"), ("0.8800", "0.88"), ("0.045", "0.045"), ("0.5", "0.50"),
])
def test_px(x, out):
    assert fmt.px(x) == out


@pytest.mark.parametrize("raw,out", [("55", "0.55"), ("55%", "0.55"), ("0.55", "0.55"), ("1.2", "1.2")])
def test_parse_iv(raw, out):
    assert fmt.parse_iv(raw) == D(out)


@pytest.mark.parametrize("raw", ["0", "500", "abc", "nan"])
def test_parse_iv_bad(raw):
    with pytest.raises(ValueError):
        fmt.parse_iv(raw)


def test_to_dec_comma():
    assert fmt.to_dec(" 1,5 ") == D("1.5")


def test_short_symbol():
    assert fmt.short_symbol("BTC-27NOV26-60000-P-USDT") == "BTC-27NOV26-60000-P"


def test_env_banner():
    assert "MAINNET" in fmt.env_banner("mainnet") and fmt.env_banner("demo") == "[demo]"


def test_settings_is_frozen():
    with pytest.raises(Exception):
        Settings().env = "mainnet"
