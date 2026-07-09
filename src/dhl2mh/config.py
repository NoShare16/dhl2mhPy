from typing import Literal

from pydantic import BaseModel, EmailStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "prod"]


class PlentySettings(BaseModel):
    username: str
    password: str
    base_url: str = "https://plenty.mykitchens.de"


class ShopwareSettings(BaseModel):
    client_id: str
    client_secret: str
    base_url: str = "https://mykitchens.de"


class DhlSettings(BaseModel):
    uat_username: str
    uat_password: str
    prod_username: str
    prod_password: str
    label_wait_seconds: int = 180
    receiving_party_id: str = "DELIVERIT"

    uat_base_url: str = "https://deliverit-uat.dhl.com/webdsi/rest/latest"
    prod_base_url: str = "https://deliverit.dhl.com/webdsi/rest/latest"

    # Sender/PartnerId/Id in the DHL XML — DHL assigns one ID per environment
    # (the user originally documented: "3 = 002 in production, 1 für UAT").
    uat_sender_partner_id: str = "1"
    prod_sender_partner_id: str = "3"


class SmtpSettings(BaseModel):
    host: str
    port: int = 587
    username: str
    password: str
    from_email: EmailStr
    from_name: str = "DHL Label Service"


class WebSettings(BaseModel):
    """Credentials for the manual-trigger web UI (optional, cron-only installs
    can leave these empty). ``secret_key`` signs the login session cookie; when
    empty it falls back to the password so a set password is always enough."""

    username: str = ""
    password: str = ""
    secret_key: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_env: Environment = "dev"
    report_recipient_email: EmailStr

    plenty: PlentySettings
    shopware: ShopwareSettings
    dhl: DhlSettings
    smtp: SmtpSettings
    web: WebSettings = WebSettings()

    @property
    def dhl_username(self) -> str:
        return self.dhl.prod_username if self.app_env == "prod" else self.dhl.uat_username

    @property
    def dhl_password(self) -> str:
        return self.dhl.prod_password if self.app_env == "prod" else self.dhl.uat_password

    @property
    def dhl_base_url(self) -> str:
        return self.dhl.prod_base_url if self.app_env == "prod" else self.dhl.uat_base_url

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached singleton. Reads .env on first call."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
