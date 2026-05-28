from dhl2mh.config import Settings


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "APP_ENV": "dev",
        "REPORT_RECIPIENT_EMAIL": "ops@example.com",
        "PLENTY__USERNAME": "u",
        "PLENTY__PASSWORD": "p",
        "SHOPWARE__CLIENT_ID": "cid",
        "SHOPWARE__CLIENT_SECRET": "csec",
        "DHL__UAT_USERNAME": "HDE",
        "DHL__UAT_PASSWORD": "uatpw",
        "DHL__PROD_USERNAME": "HDE",
        "DHL__PROD_PASSWORD": "prodpw",
        "SMTP__HOST": "smtp.example.com",
        "SMTP__PORT": "587",
        "SMTP__USERNAME": "smtp-user",
        "SMTP__PASSWORD": "smtp-pw",
        "SMTP__FROM_EMAIL": "from@example.com",
    }
    base.update(overrides)
    return base


def test_dev_picks_uat_credentials(monkeypatch):
    for k, v in _env(APP_ENV="dev").items():
        monkeypatch.setenv(k, v)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.is_production is False
    assert s.dhl_username == "HDE"
    assert s.dhl_password == "uatpw"
    assert "uat" in s.dhl_base_url


def test_prod_picks_prod_credentials(monkeypatch):
    for k, v in _env(APP_ENV="prod").items():
        monkeypatch.setenv(k, v)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.is_production is True
    assert s.dhl_password == "prodpw"
    assert "uat" not in s.dhl_base_url
