"""Order filter — pure pass/skip predicates. No mutation, no enrichment.

Returns the orders that should continue down the pipeline plus a list of
SkippedOrder records for the report mail. Service resolution (MatchCodes,
auto-attached SWG/VPR) happens later in service_resolver.py.
"""

from typing import NamedTuple

from dhl2mh.bundles import group_by_bundle, split_articles_and_services
from dhl2mh.mapping import STOCK_LIMITATION_ARTICLE
from dhl2mh.models import OrderItem, PlentyOrder, SkippedOrder


class FilterResult(NamedTuple):
    passed: list[PlentyOrder]
    skipped: list[SkippedOrder]


def filter_orders(orders: list[PlentyOrder]) -> FilterResult:
    passed: list[PlentyOrder] = []
    skipped: list[SkippedOrder] = []

    for order in orders:
        reason = _why_skip(order)
        if reason is None:
            passed.append(order)
        else:
            skipped.append(_to_skipped(order, reason))

    return FilterResult(passed=passed, skipped=skipped)


# ── predicate ──────────────────────────────────────────────────────────────


def _why_skip(order: PlentyOrder) -> str | None:
    if order.package_number:
        return f"PackageNumber vorhanden: {order.package_number}"

    if order.type_id != 1:
        return f"Kein normaler Auftrag (TypeId: {order.type_id})"

    article_count = 0
    for bundle in group_by_bundle(order.order_items):
        articles, services = split_articles_and_services(bundle)

        if services and not articles:
            return "Service-Bundle ohne Artikel"

        if len(articles) > 1:
            bid = bundle[0].former_parent_id or "?"
            return f"Bundle '{bid}' enthält mehrere Artikel"

        article_count += len(articles)

    if article_count == 0:
        return "Keine Artikel im Auftrag"

    missing_weight = _article_without_weight(order.order_items)
    if missing_weight is not None:
        return f"Artikel ohne Gewichtsangabe: {missing_weight.id}"

    return None


def _article_without_weight(items: list[OrderItem]) -> OrderItem | None:
    for item in items:
        if item.stock_limitation not in STOCK_LIMITATION_ARTICLE:
            continue
        if item.weight_g is None or item.weight_g == 0:
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
