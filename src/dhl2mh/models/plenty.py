from datetime import datetime
from decimal import Decimal

from pydantic import Field

from dhl2mh.models.base import ApiModel

# ──────────────────────────────────────────────────────────────────────────────
# Plenty API DTOs — raw JSON shape from /rest/orders/search and /shipping/countries
# Plenty returns camelCase keys; alias_generator handles that automatically.
# Field set is 1:1 with the C# DTOs in Models/PlentyApi/PlentyOrderResponse.cs.
# The bundle/group id comes from item property typeId=1021 (see mapping.plenty).
# ──────────────────────────────────────────────────────────────────────────────


class ApiVariation(ApiModel):
    stock_limitation: int = 0
    weight_g: int = 0
    # Plenty "Variantennummer". On second-choice (B-Ware) variations it already
    # carries the readable model designation, which the Akeneo PIM cannot supply
    # under the B-Ware variation id — see ``pipeline._apply_second_choice``.
    number: str | None = None
    # Plenty uses widthMM/lengthMM/heightMM (capital MM), which alias_generator
    # would otherwise turn into widthMm/lengthMm/heightMm — override explicitly.
    width_mm: int = Field(default=0, alias="widthMM")
    length_mm: int = Field(default=0, alias="lengthMM")
    height_mm: int = Field(default=0, alias="heightMM")


class ApiProperty(ApiModel):
    type_id: int
    value: str | None = None


class ApiOrderItem(ApiModel):
    type_id: int
    item_variation_id: int = 0
    order_item_name: str | None = None
    quantity: Decimal = Decimal(0)
    variation: ApiVariation | None = None
    # Item-level properties — typeId=1021 carries the bundle/group id
    properties: list[ApiProperty] = Field(default_factory=list)


class ApiAddressOption(ApiModel):
    type_id: int
    value: str | None = None


class ApiAddress(ApiModel):
    id: int
    name2: str | None = None
    name3: str | None = None
    address1: str | None = None
    address2: str | None = None
    postal_code: str | None = None
    town: str | None = None
    country_id: int = 0
    options: list[ApiAddressOption] = Field(default_factory=list)


class ApiAddressRelation(ApiModel):
    type_id: int
    address_id: int


class ApiRelation(ApiModel):
    reference_type: str | None = None
    reference_id: int = 0
    relation: str | None = None


class ApiShippingPackage(ApiModel):
    package_number: str | None = None


class ApiOrder(ApiModel):
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


class ApiOrderPage(ApiModel):
    is_last_page: bool = True
    entries: list[ApiOrder] = Field(default_factory=list)


class ApiCountry(ApiModel):
    id: int
    iso_code2: str | None = Field(default=None, alias="isoCode2")

