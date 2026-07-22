"""Match Shopware order line items onto Plenty order positions.

The Shopware order (fetched by orderNumber) carries
``dvsnProductOptionFormerParentId`` per line item. Plenty positions are matched
to those line items by ``productNumber == str(OrderItem.id)`` — the same key the
category enrichment already uses (Plenty itemVariationId == Shopware
productNumber).

The match is one-to-many: Plenty aggregates line items that share a
productNumber into a single position, so a position whose line items point at
different parents is split back apart (see ``assign_former_parent_ids``).
"""

from decimal import Decimal
from typing import NamedTuple

from dhl2mh.bundles import is_service
from dhl2mh.mapping import (
    COLOR_GROUP_ID,
    SHOPWARE_PRODUCT_NUMBER_ALIASES,
    WATER_CONNECTION_GROUP_ID,
)
from dhl2mh.models import (
    OrderItem,
    PlentyOrder,
    SkippedOrder,
    SwOrder,
    SwProduct,
    SwProductInfo,
)


class FormerParentAssignment(NamedTuple):
    matched: int
    split: int


def assign_former_parent_ids(
    order: PlentyOrder, sw_order: SwOrder
) -> FormerParentAssignment:
    """Copy each line item's formerParentId onto the matching Plenty position.

    A Plenty position is the *aggregate* of every Shopware line item sharing its
    productNumber: the same service ordered for two articles arrives as two
    Shopware line items — one per parent — but as a single Plenty position of
    quantity 2. Such a position is split into one position per formerParentId
    (quantity taken from the Shopware line items), so every parent keeps its own
    article and its own services. Without the split the last line item would win
    and one article would silently lose its services.

    Replaces ``order.order_items``. Returns how many positions matched a Shopware
    line item, and how many of those had to be split.
    """
    parents = _parents_by_product_number(sw_order)
    items: list[OrderItem] = []
    matched = 0
    split = 0

    for item in order.order_items:
        groups = _parents_for(item, parents)
        if not groups:
            items.append(item)
            continue
        matched += 1
        if len(groups) > 1:
            split += 1
        items.extend(_apply_parents(item, groups))

    order.order_items = items
    return FormerParentAssignment(matched=matched, split=split)


def _parents_by_product_number(sw_order: SwOrder) -> dict[str, dict[str, Decimal]]:
    """productNumber → {formerParentId: summed quantity}, in line-item order.

    Line items without a productNumber (promotions) or without a formerParentId
    are dropped — an absent Shopware value must never clear a Plenty-seeded id.
    """
    parents: dict[str, dict[str, Decimal]] = {}
    for li in sw_order.line_items:
        product_number = li.payload.product_number
        former_parent_id = li.payload.dvsn_product_option_former_parent_id
        if not product_number or not former_parent_id:
            continue
        groups = parents.setdefault(product_number, {})
        groups[former_parent_id] = groups.get(former_parent_id, Decimal(0)) + li.quantity
    return parents


def _parents_for(
    item: OrderItem, parents: dict[str, dict[str, Decimal]]
) -> dict[str, Decimal] | None:
    """Line-item parents for a Plenty position — by variation id, else by alias."""
    groups = parents.get(str(item.id))
    if groups is not None:
        return groups
    alias = SHOPWARE_PRODUCT_NUMBER_ALIASES.get(item.id)
    return parents.get(alias) if alias else None


def _apply_parents(item: OrderItem, groups: dict[str, Decimal]) -> list[OrderItem]:
    """One position per formerParentId; a single parent keeps the original item."""
    if len(groups) == 1:
        item.former_parent_id = next(iter(groups))
        return [item]
    return [
        item.model_copy(
            update={"former_parent_id": former_parent_id, "quantity": qty, "packages": qty},
            deep=True,
        )
        for former_parent_id, qty in groups.items()
    ]


def assign_water_connection(order: PlentyOrder, sw_order: SwOrder) -> int:
    """Set ``festwasser`` on each article from its Shopware product property.

    Reads the "Wasseranschluss" property group (value "ja"/"nein") off the
    product line items and applies it to the matching Plenty position by
    productNumber. Returns the number of positions updated.
    """
    flags = {
        li.product.product_number: _water_connection_flag(li.product)
        for li in sw_order.line_items
        if li.product is not None and li.product.product_number
    }
    matched = 0
    for item in order.order_items:
        flag = flags.get(str(item.id))
        if flag is not None:
            item.festwasser = flag
            matched += 1
    return matched


def product_display_name(info: SwProductInfo, *, fallback: str | None) -> str | None:
    """DHL ProductName from the Shopware product: ``manufacturerNumber`` + color.

    Both parts must be present to build the combined name; if either is missing,
    the ``fallback`` (the Plenty order_item_name) is kept unchanged.
    """
    manufacturer = (info.manufacturer_number or "").strip()
    color = (info.color(COLOR_GROUP_ID) or "").strip()
    if manufacturer and color:
        return f"{manufacturer} {color}"
    return fallback


def _water_connection_flag(product: SwProduct) -> bool | None:
    """True/False from the Wasseranschluss property, or None if absent."""
    for prop in product.properties:
        if prop.group_id == WATER_CONNECTION_GROUP_ID:
            return (prop.name or "").strip().lower() == "ja"
    return None


class FormerParentResult(NamedTuple):
    passed: list[PlentyOrder]
    skipped: list[SkippedOrder]


def require_service_former_parent_ids(orders: list[PlentyOrder]) -> FormerParentResult:
    """Split orders on the former_parent_id requirement.

    former_parent_id is mandatory on every real service position (whitelisted
    service, see ``is_service``). If an order has such services and at least one
    still lacks the id (neither Plenty property 1021 nor Shopware provided one),
    the whole order is skipped. Orders without services — or with only discount
    positions — are unaffected.
    """
    passed: list[PlentyOrder] = []
    skipped: list[SkippedOrder] = []

    for order in orders:
        missing = _service_without_former_parent(order)
        if missing is None:
            passed.append(order)
        else:
            skipped.append(
                _to_skipped(order, f"Serviceposition ohne FormerParentId: {missing.id}")
            )

    return FormerParentResult(passed=passed, skipped=skipped)


def _service_without_former_parent(order: PlentyOrder) -> OrderItem | None:
    for item in order.order_items:
        if is_service(item) and not item.former_parent_id:
            return item
    return None


def _to_skipped(order: PlentyOrder, reason: str) -> SkippedOrder:
    name = order.addresses[0].full_name if order.addresses else ""
    return SkippedOrder(
        order_id=order.id,
        order_date=order.order_date,
        reason=reason,
        customer_name=name or "N/A",
        item_count=len(order.order_items),
    )
