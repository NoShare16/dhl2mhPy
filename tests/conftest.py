import pytest

from dhl2mh.config import Settings


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Settings populated from env vars, ignoring any .env file."""
    env = {
        "APP_ENV": "dev",
        "REPORT_RECIPIENT_EMAIL": "ops@example.com",
        "PLENTY__USERNAME": "plenty-user",
        "PLENTY__PASSWORD": "plenty-pw",
        "PLENTY__BASE_URL": "https://plenty.test",
        "SHOPWARE__CLIENT_ID": "cid",
        "SHOPWARE__CLIENT_SECRET": "csec",
        "SHOPWARE__BASE_URL": "https://shopware.test",
        "DHL__UAT_USERNAME": "HDE",
        "DHL__UAT_PASSWORD": "uatpw",
        "DHL__PROD_USERNAME": "HDE",
        "DHL__PROD_PASSWORD": "prodpw",
        "DHL__UAT_BASE_URL": "https://dhl-uat.test/webdsi/rest/latest",
        "DHL__PROD_BASE_URL": "https://dhl-prod.test/webdsi/rest/latest",
        "SMTP__HOST": "smtp.test",
        "SMTP__PORT": "587",
        "SMTP__USERNAME": "smtp-user",
        "SMTP__PASSWORD": "smtp-pw",
        "SMTP__FROM_EMAIL": "from@test.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]
