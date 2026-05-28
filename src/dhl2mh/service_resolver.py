"""Resolve services per article: collect bundle services, map to DHL MatchCodes,
auto-attach SWG (heavy-lift) and VPR (trigger-code based), and derive weight_kg
plus volume_cbm. Mutates articles in place.

Runs after filter (bundle structure is already valid: exactly 1 article per bundle)
and after Shopware category enrichment (article.categories must be populated, used
by the IS/E-AN decision for SERVICE_INSTALL).
"""

from decimal import Decimal
from typing import NamedTuple

from dhl2mh.bundles import group_by_bundle, split_articles_and_services
from dhl2mh.mapping import (
    HEAVY_LIFT_SERVICE_ID,
    HEAVY_LIFT_THRESHOLD_KG,
    VPR_MATCH_CODE,
    VPR_SERVICE_ID,
    VPR_TRIGGER_MATCH_CODES,
    UnknownServiceIdError,
    map_to_match_codes,
)
from dhl2mh.models import OrderItem, PlentyOrder, SkippedOrder


class ResolveResult(NamedTuple):
    passed: list[PlentyOrder]
    skipped: list[SkippedOrder]


def resolve_orders(orders: list[PlentyOrder]) -> ResolveResult:
    """Resolve services for every order. Unknown service IDs cause that order
    to be skipped (returned in ``skipped``)."""
    passed: list[PlentyOrder] = []
    skipped: list[SkippedOrder] = []

    for order in orders:
        try:
            resolve_order(order)
        except UnknownServiceIdError as e:
            skipped.append(_to_skipped(order, str(e)))
        else:
            passed.append(order)

    return ResolveResult(passed=passed, skipped=skipped)


def resolve_order(order: PlentyOrder) -> PlentyOrder:
    """Mutates each article in-place: sets service_ids, service_match_codes,
    weight_kg, volume_cbm. Returns the same order for convenience."""
    for bundle in group_by_bundle(order.order_items):
        articles, services = split_articles_and_services(bundle)
        if not articles:
            # Filter guarantees this doesn't happen, but stay defensive.
            continue
        # Filter guarantees exactly one article per bundle.
        _resolve_article(articles[0], services)
    return order


# ── per-article logic ──────────────────────────────────────────────────────


def _resolve_article(article: OrderItem, services: list[OrderItem]) -> None:
    service_ids: list[int] = [s.id for s in services]

    if _is_heavy_lift(article) and HEAVY_LIFT_SERVICE_ID not in service_ids:
        service_ids.append(HEAVY_LIFT_SERVICE_ID)

    match_codes: list[str] = []
    for sid in service_ids:
        match_codes.extend(map_to_match_codes(sid, article.categories))

    if VPR_MATCH_CODE not in match_codes and any(
        code in VPR_TRIGGER_MATCH_CODES for code in match_codes
    ):
        service_ids.append(VPR_SERVICE_ID)
        match_codes.append(VPR_MATCH_CODE)

    article.service_ids = service_ids
    article.service_match_codes = match_codes
    article.weight_kg = _g_to_kg(article.weight_g)
    article.volume_cbm = _volume_cbm(article)


def _is_heavy_lift(article: OrderItem) -> bool:
    weight_kg = _g_to_kg(article.weight_g)
    return weight_kg is not None and weight_kg > HEAVY_LIFT_THRESHOLD_KG


def _g_to_kg(weight_g: Decimal | None) -> Decimal | None:
    if weight_g is None or weight_g == 0:
        return None
    return (weight_g / Decimal(1000)).quantize(Decimal("0.01"))


def _volume_cbm(item: OrderItem) -> Decimal:
    if item.width_mm == 0 or item.length_mm == 0 or item.height_mm == 0:
        return Decimal(0)
    cbm = Decimal(item.width_mm * item.length_mm * item.height_mm) / Decimal("1000000000")
    return cbm.quantize(Decimal("0.001"))


def _to_skipped(order: PlentyOrder, reason: str) -> SkippedOrder:
    name = order.addresses[0].full_name if order.addresses else ""
    return SkippedOrder(
        order_id=order.id,
        order_date=order.order_date,
        reason=reason,
        customer_name=name or "N/A",
        item_count=len(order.order_items),
    )
