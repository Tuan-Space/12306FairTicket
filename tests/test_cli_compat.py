from datetime import date, timedelta

import main as cli
from ticket_app.configuration import AppError


def test_cli_validate_config_keeps_the_original_entry_point(tmp_path, monkeypatch):
    future_date = (date.today() + timedelta(days=1)).isoformat()
    config_path = tmp_path / "config.py"
    config_path.write_text(
        "\n".join(
            (
                'FROM_STATION = "北京南"',
                'TO_STATION = "上海虹桥"',
                f'TRAIN_DATE = "{future_date}"',
                'PASSENGER_NAMES = ["张三"]',
                'SEAT_TYPES = ["二等座"]',
                'PREFERRED_TRAINS = []',
                'ONLY_PREFERRED_TRAINS = False',
                'START_AT = ""',
                'STOP_AT = ""',
                'AUTO_SUBMIT = False',
                'CHOOSE_SEATS = ""',
            )
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: None)
    assert cli.main(["--config", str(config_path), "--validate-config"]) == 0


def test_cli_keeps_passenger_redaction_terms_when_runner_raises(tmp_path, monkeypatch):
    future_date = (date.today() + timedelta(days=1)).isoformat()
    config_path = tmp_path / "config.py"
    config_path.write_text(
        "\n".join(
            (
                'FROM_STATION = "北京南"',
                'TO_STATION = "上海虹桥"',
                f'TRAIN_DATE = "{future_date}"',
                'PASSENGER_NAMES = ["张三"]',
                'SEAT_TYPES = ["二等座"]',
                'PREFERRED_TRAINS = []',
                'ONLY_PREFERRED_TRAINS = False',
                'START_AT = ""',
                'STOP_AT = ""',
                'AUTO_SUBMIT = False',
            )
        ),
        encoding="utf-8",
    )

    class FailingRunner:
        def __init__(self, _cfg):
            pass

        def run(self):
            raise AppError("未找到乘车人: 张三")

    monkeypatch.setattr(cli, "TicketRunner", FailingRunner)
    configured = []
    monkeypatch.setattr(
        cli,
        "configure_logging",
        lambda level, sensitive_terms=(): configured.append((level, tuple(sensitive_terms))),
    )
    assert cli.main(["--config", str(config_path)]) == 2
    assert configured[-1] == ("INFO", ("张三",))


def test_cli_rejects_empty_exact_train_list_before_initializing_runner(tmp_path, monkeypatch):
    future_date = (date.today() + timedelta(days=1)).isoformat()
    config_path = tmp_path / "config.py"
    config_path.write_text(
        f'FROM_STATION = "北京南"\nTO_STATION = "上海虹桥"\nTRAIN_DATE = "{future_date}"\n'
        'PASSENGER_NAMES = []\nSEAT_TYPES = ["二等座"]\nPREFERRED_TRAINS = []\n'
        'ONLY_PREFERRED_TRAINS = True\nAUTO_SUBMIT = False\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: None)

    def unexpected_runner(_cfg):
        raise AssertionError("invalid scope must be rejected before querying or login")

    monkeypatch.setattr(cli, "TicketRunner", unexpected_runner)
    assert cli.main(["--config", str(config_path), "--validate-config"]) == 2
