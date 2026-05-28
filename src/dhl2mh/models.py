from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ──────────────────────────────────────────────────────────────────────────────
# Plenty API DTOs — raw JSON shape from /rest/orders/search and /shipping/countries
# Plenty returns camelCase keys; alias_generator handles that automatically.
# Field set is 1:1 with the C# DTOs in Models/PlentyApi/PlentyOrderResponse.cs.
# Real BundleId/GroupId field name is still unknown — added once we have a fixture.
# ──────────────────────────────────────────────────────────────────────────────


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class ApiVariation(_ApiModel):
    stock_limitation: int = 0
    weight_g: int = 0
    # Plenty uses widthMM/lengthMM/heightMM (capital MM), which alias_generator
    # would otherwise turn into widthMm/lengthMm/heightMm — override explicitly.
    width_mm: int = Field(default=0, alias="widthMM")
    length_mm: int = Field(default=0, alias="lengthMM")
    height_mm: int = Field(default=0, alias="heightMM")


class ApiProperty(_ApiModel):
    type_id: int
    value: str | None = None


class ApiOrderItem(_ApiModel):
    type_id: int
    item_variation_id: int = 0
    order_item_name: str | None = None
    quantity: Decimal = Decimal(0)
    variation: ApiVariation | None = None
    # Item-level properties — typeId=1021 carries the bundle/group id
    properties: list[ApiProperty] = Field(default_factory=list)


class ApiAddressOption(_ApiModel):
    type_id: int
    value: str | None = None


class ApiAddress(_ApiModel):
    id: int
    name2: str | None = None
    name3: str | None = None
    address1: str | None = None
    address2: str | None = None
    postal_code: str | None = None
    town: str | None = None
    country_id: int = 0
    options: list[ApiAddressOption] = Field(default_factory=list)


class ApiAddressRelation(_ApiModel):
    type_id: int
    address_id: int


class ApiRelation(_ApiModel):
    reference_type: str | None = None
    reference_id: int = 0
    relation: str | None = None


class ApiShippingPackage(_ApiModel):
    package_number: str | None = None


class ApiOrder(_ApiModel):
    id: int
    status_id: float
    type_id: int
    created_at: datetime
    relations: list[ApiRelation] = Field(default_factory=list)
    address_relations: list[ApiAddressRelation] = Field(default_factory=list)
    addresses: list[ApiAddress] = Field(default_factory=list)
    order_items: list[ApiOrderItem] = Field(default_factory=list)
    shipping_packages: list[ApiShippingPackage] = Field(default_factory=list)
    properties: list[ApiProperty] = Field(default_factory=list)


class ApiOrderPage(_ApiModel):
    is_last_page: bool = True
    entries: list[ApiOrder] = Field(default_factory=list)


class ApiCountry(_ApiModel):
    id: int
    iso_code2: str | None = Field(default=None, alias="isoCode2")


# ──────────────────────────────────────────────────────────────────────────────
# Domain models — what the pipeline (filter, xml_builder, dhl_client) operates on
# ──────────────────────────────────────────────────────────────────────────────


class Address(BaseModel):
    id: int
    customer_id: int = 0
    first_name: str | None = None
    last_name: str | None = None
    country_code: str | None = None
    postal_code: str | None = None
    city: str | None = None
    street: str | None = None
    phone_number: str | None = None
    email: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name or ''} {self.last_name or ''}".strip()


class OrderItem(BaseModel):
    id: int
    name: str | None = None
    quantity: Decimal | None = None
    stock_limitation: int = 0

    # Bundle/group key from Plenty property typeId=1021. Items sharing the same
    # bundle_id belong together: typically one article (StockLimitation 0/1) plus
    # zero or more services (StockLimitation 2).
    bundle_id: str | None = None

    # Resolved during filter stage
    service_ids: list[int] = Field(default_factory=list)
    service_match_codes: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    packages: Decimal | None = None
    weight_kg: Decimal | None = None
    volume_cbm: Decimal | None = None

    # Raw measurements from Plenty variation (used to derive weight_kg/volume_cbm)
    weight_g: Decimal | None = None
    height_mm: int = 0
    length_mm: int = 0
    width_mm: int = 0


class PlentyOrder(BaseModel):
    id: int
    status_id: float
    type_id: int
    order_date: datetime
    addresses: list[Address] = Field(default_factory=list)
    order_items: list[OrderItem] = Field(default_factory=list)
    package_number: str | None = None
    shopware_id: str | None = None


class SkippedOrder(BaseModel):
    order_id: int
    order_date: datetime
    reason: str
    customer_name: str = "N/A"
    item_count: int = 0


class LabelInfo(BaseModel):
    """DHL transmissionStatus payload, slimmed down: tracking number only.

    Source XML carries Base64 PDF + Stamp + DocumentType, but we no longer
    download/merge labels — only OrderIdent gets pushed back to Plenty.
    """

    order_id: int
    order_ident: str
    barcode: str = ""


class PackageData(BaseModel):
    package_id: int = 1
    package_number: str
    package_type: int = 0
