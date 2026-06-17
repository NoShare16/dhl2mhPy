"""Match Shopware order line items onto Plenty order positions.

The Shopware order (fetched by orderNumber) carries
``dvsnProductOptionFormerParentId`` per line item. Plenty positions are matched
to those line items by ``productNumber == str(OrderItem.id)`` — the same key the
category enrichment already uses (Plenty itemVariationId == Shopware
productNumber).
"""

from typing import NamedTuple

from dhl2mh.mapping import STOCK_LIMITATION_SERVICE
from dhl2mh.models import OrderItem, PlentyOrder, SkippedOrder, SwOrder


def assign_former_parent_ids(order: PlentyOrder, sw_order: SwOrder) -> int:
    """Copy each line item's formerParentId onto the matching Plenty position.

    Mutates ``order.order_items`` in place. Returns the number of positions that
    were matched (for logging / diagnostics).
    """
    by_product_number = {
        li.payload.product_number: li.payload.dvsn_product_option_former_parent_id
        for li in sw_order.line_items
        if li.payload.product_number
        and li.payload.dvsn_product_option_former_parent_id
    }
    matched = 0
    for item in order.order_items:
        former_parent_id = by_product_number.get(str(item.id))
        if former_parent_id is not None:
            item.former_parent_id = former_parent_id
            matched += 1
    return matched


class FormerParentResult(NamedTuple):
    passed: list[PlentyOrder]
    skipped: list[SkippedOrder]


def require_service_former_parent_ids(orders: list[PlentyOrder]) -> FormerParentResult:
    """Split orders on the former_parent_id requirement.

    former_parent_id is mandatory on every service position (StockLimitation==2).
    If an order has services and at least one of them still lacks the id (neither
    Plenty property 1021 nor Shopware provided one), the whole order is skipped.
    Orders without services are unaffected.
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
        if item.stock_limitation == STOCK_LIMITATION_SERVICE and not item.former_parent_id:
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
