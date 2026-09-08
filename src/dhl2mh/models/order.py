from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

# ──────────────────────────────────────────────────────────────────────────────
# Domain models — what the pipeline (domain.filter, mapping.xml_builder,
# clients.dhl) operates on
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

    # True when this position is a Plenty item-bundle PARENT (order-item typeId 2).
    # A service bundle (e.g. 783117, stock_limitation 2) is folded into its article
    # like any other service; an article bundle (stock_limitation 0/1) is currently
    # unsupported and gets the whole order skipped by the filter.
    is_bundle_parent: bool = False

    # Bundle/group key from Plenty property typeId=1021. Items sharing the same
    # bundle_id belong together: typically one article (StockLimitation 0/1) plus
    # zero or more services (StockLimitation 2).
    bundle_id: str | None = None

    # Resolved during filter stage
    service_ids: list[int] = Field(default_factory=list)
    service_match_codes: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    # Grouping/parent key used to fold services into their article (group_by_bundle).
    # Seeds from bundle_id (Plenty property 1021), then gets overwritten by the
    # Shopware dvsnProductOptionFormerParentId where one exists (matched via
    # productNumber == str(id) during the Shopware order enrichment).
    former_parent_id: str | None = None

    # Article needs a fixed water connection (Shopware property group
    # "Wasseranschluss" == "ja"). Flips SERVICE_INSTALL towards AWS.
    festwasser: bool = False

    # True once ``name`` holds a real model designation — either the Akeneo
    # "modell" attribute or Shopware's manufacturerNumber + color. Stays False
    # while ``name`` is only the Plenty order_item_name, which is not a model
    # number: such orders are skipped rather than shipped
    # (domain.filter.require_model_names).
    has_model_name: bool = False

    # Article is second choice — the Shopware product carries the "B-Ware" tag.
    # Its ``name`` is rebuilt from ``variation_number`` and gets the "[ZW]"
    # prefix once both name sources have run (pipeline._apply_second_choice).
    second_choice: bool = False

    # Plenty "Variantennummer" (variation.number). Only consulted for
    # second-choice articles, where it is the authoritative model designation —
    # the Akeneo PIM knows the original variation id, not the B-Ware one.
    variation_number: str | None = None

    @model_validator(mode="after")
    def _seed_former_parent_id(self) -> "OrderItem":
        if self.former_parent_id is None:
            self.former_parent_id = self.bundle_id
        return self

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


class AckError(BaseModel):
    """One rejected order from a DHL acknowledgement.

    DeliverIT answers an upload with HTTP 200 and reports rejections only inside
    the ``TransmissionAcknowledgement`` body: ``AcknowledgementDetails`` carries
    ``ErrorCode``/``ErrorResponse`` next to the echoed order. Details without an
    ``ErrorCode`` are acceptances, not errors.
    """

    order_id: int | None = None
    error_code: str
    error_text: str = ""


class PackageData(BaseModel):
    package_id: int = 1
    package_number: str
    package_type: int = 0
