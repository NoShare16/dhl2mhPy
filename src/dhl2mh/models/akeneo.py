from pydantic import BaseModel, ConfigDict, Field

# ──────────────────────────────────────────────────────────────────────────────
# Akeneo PIM DTOs — /api/rest/v1/products. Every value is a list of
# {locale, scope, data} entries; the attributes we read are neither localizable
# nor scopable, so each list holds exactly one entry.
# ──────────────────────────────────────────────────────────────────────────────


class AkeneoValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    locale: str | None = None
    scope: str | None = None
    data: object = None


class AkeneoProduct(BaseModel):
    """Raw product entry, reduced to the attribute values we read."""

    model_config = ConfigDict(extra="ignore")

    identifier: str | None = None
    values: dict[str, list[AkeneoValue]] = Field(default_factory=dict)

    def scalar(self, attribute: str) -> str | None:
        """First non-empty scalar value of an attribute, as a string.

        Returns ``None`` when the attribute is absent, empty, or holds a
        composite value (media, price collections) — none of which the
        ProductName is built from.
        """
        for value in self.values.get(attribute) or []:
            data = value.data
            if isinstance(data, str) and data.strip():
                return data.strip()
            if isinstance(data, bool):
                continue
            if isinstance(data, int | float):
                return str(data)
        return None


class AkeneoProductInfo(BaseModel):
    """Resolved PIM data for one Plenty variation.

    ``color`` is already the de_DE option *label* — the product API only returns
    the option code, which the client resolves before handing it on.
    """

    model: str | None = None
    color: str | None = None

