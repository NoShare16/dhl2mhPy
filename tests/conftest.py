import pytest

from dhl2mh.config import Settings


@pytest.fixture
def settings(monkeypatch, tmp_path) -> Settings:
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
        # Keep archived acknowledgements out of the working directory.
        "DHL__ACK_ARCHIVE_DIR": str(tmp_path / "acks"),
        "SMTP__HOST": "smtp.test",
        "SMTP__PORT": "587",
        "SMTP__USERNAME": "smtp-user",
        "SMTP__PASSWORD": "smtp-pw",
        "SMTP__FROM_EMAIL": "from@test.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def akeneo_settings(settings, monkeypatch) -> Settings:
    """Settings with both PIM instances configured (MK first, then ML)."""
    env = {
        "AKENEO__USERNAME": "pim-user",
        "AKENEO__PASSWORD": "pim-pw",
        "AKENEOMK__BASE_URL": "https://pim-mk.test",
        "AKENEOMK__CLIENT_ID": "mk-cid",
        "AKENEOMK__SECRET": "mk-secret",
        "AKENEOML__BASE_URL": "https://pim-ml.test",
        "AKENEOML__CLIENT_ID": "ml-cid",
        "AKENEOML__SECRET": "ml-secret",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]
