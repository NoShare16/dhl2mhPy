"""Bundle grouping for order items — shared by filter and service resolver.

A "bundle" is a set of items sharing the same ``former_parent_id``: typically one
article (StockLimitation 0/1) plus zero or more services (StockLimitation 2).
``former_parent_id`` is the Plenty bundle id (property 1021) by default, but
Shopware's dvsnProductOptionFormerParentId overrides it when present — so the
grouping follows the Shopware parent for shop orders. Items without a
former_parent_id form single-item bundles of their own.
"""

from collections import OrderedDict

from dhl2mh.mapping import (
    SERVICE_WHITELIST,
    STOCK_LIMITATION_ARTICLE,
    STOCK_LIMITATION_SERVICE,
)
from dhl2mh.models import OrderItem


def is_service(item: OrderItem) -> bool:
    """A real DHL service: StockLimitation 2 *and* a whitelisted service id.

    Other StockLimitation-2 positions (rebates/discounts like "2% Rabatt",
    "Nachlass") carry stock_limitation 2 in Plenty but are NOT services — they
    are ignored everywhere (not articles, not services, no effect on bundles).
    """
    return (
        item.stock_limitation == STOCK_LIMITATION_SERVICE
        and item.id in SERVICE_WHITELIST
    )


def group_by_bundle(items: list[OrderItem]) -> list[list[OrderItem]]:
    """Group items into bundles. Preserves first-seen order across groups.

    Items with the same non-None ``former_parent_id`` end up in one group; items
    without a ``former_parent_id`` each form their own single-item group.
    """
    groups: OrderedDict[object, list[OrderItem]] = OrderedDict()
    standalone_counter = 0

    for item in items:
        if item.former_parent_id is None:
            key: object = ("standalone", standalone_counter)
            standalone_counter += 1
        else:
            key = ("bundle", item.former_parent_id)
        groups.setdefault(key, []).append(item)

    return list(groups.values())


def split_articles_and_services(
    group: list[OrderItem],
) -> tuple[list[OrderItem], list[OrderItem]]:
    """Split a bundle into (articles, services).

    Non-service StockLimitation-2 positions (discounts) fall into neither list.
    """
    articles = [i for i in group if i.stock_limitation in STOCK_LIMITATION_ARTICLE]
    services = [i for i in group if is_service(i)]
    return articles, services
