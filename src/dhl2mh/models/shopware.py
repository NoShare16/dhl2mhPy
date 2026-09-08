from decimal import Decimal

from pydantic import Field, field_validator

from dhl2mh.models.base import ApiModel

# ──────────────────────────────────────────────────────────────────────────────
# Shopware API DTOs — /api/search/order with Accept: application/json (the
# flattened, non-JSON:API shape). Only the fields we map are modelled.
# ──────────────────────────────────────────────────────────────────────────────


class SwLineItemPayload(ApiModel):
    product_number: str | None = None
    # The Shopware product id this line item was split off from (services point
    # back to their parent article). This is the field we ultimately map onto
    # Plenty order positions via the variant id.
    dvsn_product_option_former_parent_id: str | None = None


class SwPropertyOption(ApiModel):
    """A product property value, e.g. group "Wasseranschluss" → name "ja"/"nein"."""

    name: str | None = None
    group_id: str | None = None


class SwProduct(ApiModel):
    product_number: str | None = None
    properties: list[SwPropertyOption] = Field(default_factory=list)

    @field_validator("properties", mode="before")
    @classmethod
    def _null_to_empty(cls, v: object) -> object:
        # Shopware sends properties: null when the association isn't requested.
        return v or []


class SwProductInfo(ApiModel):
    """Product data from /api/search/product (Accept: application/json, flat shape).

    Fetched per article during enrichment. Carries the category ids (for the
    Herde/IS decision), the two fields the DHL ProductName is now built from
    (``manufacturerNumber`` and the color property option, matched by group),
    and the tag ids that mark an article as second choice ("B-Ware").
    """

    product_number: str | None = None
    manufacturer_number: str | None = None
    category_ids: list[str] = Field(default_factory=list)
    # Shipped by the flat product response without requesting an association.
    tag_ids: list[str] = Field(default_factory=list)
    properties: list[SwPropertyOption] = Field(default_factory=list)

    @field_validator("category_ids", "tag_ids", "properties", mode="before")
    @classmethod
    def _null_to_empty(cls, v: object) -> object:
        # Shopware sends null when the field/association isn't populated.
        return v or []

    def color(self, group_id: str) -> str | None:
        """Name of the color property option, matched by its property group."""
        return next(
            (p.name for p in self.properties if p.group_id == group_id and p.name),
            None,
        )


class SwOrderLineItem(ApiModel):
    type: str | None = None
    label: str | None = None
    # Per line item, NOT per position: the same service ordered for two articles
    # is two line items of quantity 1, which Plenty aggregates into one position
    # of quantity 2 (see mapping.shopware.assign_former_parent_ids).
    quantity: Decimal = Decimal(1)
    # referencedId = the dvsn product-option id for services, the product id for
    # real products; productId is only set for type "product".
    referenced_id: str | None = None
    product_id: str | None = None
    payload: SwLineItemPayload = Field(default_factory=SwLineItemPayload)
    # Present only for type "product"; carries the product properties.
    product: SwProduct | None = None


class SwOrder(ApiModel):
    order_number: str | None = None
    line_items: list[SwOrderLineItem] = Field(default_factory=list)

