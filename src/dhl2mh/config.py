from typing import Literal

from pydantic import BaseModel, EmailStr
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


class AkeneoInstanceSettings(BaseModel):
    """Client credentials for one Akeneo PIM instance (MK or ML).

    Empty values mean "not configured": that instance is then skipped and the
    article keeps the Shopware-derived product name.
    """

    base_url: str = ""
    client_id: str = ""
    secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.client_id and self.secret)


class AkeneoSettings(BaseModel):
    """Username/password shared by both PIM instances (OAuth password grant)."""

    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


class DhlSettings(BaseModel):
    uat_username: str
    uat_password: str
    prod_username: str
    prod_password: str
    label_wait_seconds: int = 180
    receiving_party_id: str = "DELIVERIT"

    # The acknowledgement queue is consume-once and large (UAT measured 870 KB,
    # prod is bigger): a read timeout still drains it server-side, so the answer
    # would be lost. Hence its own generous timeout, separate from the 60 s the
    # other DHL calls use.
    ack_read_timeout_seconds: float = 600.0
    # Raw acknowledgement responses are archived here before anything is parsed
    # — same reason: there is no second chance to fetch them.
    ack_archive_dir: str = "var/dhl-acknowledgements"

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

    # Akeneo PIM — optional. Shared credentials plus one client per instance,
    # so an install that leaves these empty simply keeps the Shopware name.
    akeneo: AkeneoSettings = AkeneoSettings()
    akeneomk: AkeneoInstanceSettings = AkeneoInstanceSettings()
    akeneoml: AkeneoInstanceSettings = AkeneoInstanceSettings()

    @property
    def akeneo_instances(self) -> list[tuple[str, AkeneoInstanceSettings]]:
        """Configured PIM instances in lookup order: MK first, then ML.

        Empty when the shared credentials are missing or no instance is
        configured — the Akeneo enrichment is then skipped entirely.
        """
        if not self.akeneo.configured:
            return []
        return [
            (name, instance)
            for name, instance in (("MK", self.akeneomk), ("ML", self.akeneoml))
            if instance.configured
        ]

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
