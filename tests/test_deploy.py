"""deploy/options-bot.service: пользовательская служба, пути согласованы, запускается модуль бота."""
import configparser
import importlib.util
from pathlib import Path

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "options-bot.service"


def _unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(UNIT.read_text())
    return cp


def test_unit_paths_consistent():
    svc = _unit()["Service"]
    wd = svc["WorkingDirectory"]
    assert wd.startswith("%h/")                    # без жёсткого пути и пользователя
    assert svc["ExecStart"] == f"{wd}/.venv/bin/python -m optbot.bot"
    assert svc["Restart"] == "always"
    from optbot.bot import EXIT_NO_DB
    assert svc["RestartPreventExitStatus"] == str(EXIT_NO_DB)   # БД не разделена — не перезапускать по кругу
    assert importlib.util.find_spec("optbot.bot") is not None


def test_unit_is_user_service():
    u = _unit()
    assert u["Install"]["WantedBy"] == "default.target"   # multi-user.target в --user нет
    assert "User" not in u["Service"] and "Group" not in u["Service"]
    # песочница ProtectSystem/PrivateTmp в --user требует user namespaces — на общем сервере может не стартовать
    assert not any(k.startswith(("Protect", "Private", "ReadWrite")) for k in u["Service"])
