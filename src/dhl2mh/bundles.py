"""Bundle grouping for order items — shared by filter and service resolver.

A "bundle" is a set of items sharing the same ``bundle_id`` (Plenty property
typeId=1021): typically one article (StockLimitation 0/1) plus zero or more
services (StockLimitation 2). Items without a bundle_id form single-item
bundles of their own.
"""

from collections import OrderedDict

from dhl2mh.mapping import STOCK_LIMITATION_ARTICLE, STOCK_LIMITATION_SERVICE
from dhl2mh.models import OrderItem


def group_by_bundle(items: list[OrderItem]) -> list[list[OrderItem]]:
    """Group items into bundles. Preserves first-seen order across groups.

    Items with the same non-None ``bundle_id`` end up in one group; items
    without a ``bundle_id`` each form their own single-item group.
    """
    groups: OrderedDict[object, list[OrderItem]] = OrderedDict()
    standalone_counter = 0

    for item in items:
        if item.bundle_id is None:
            key: object = ("standalone", standalone_counter)
            standalone_counter += 1
        else:
            key = ("bundle", item.bundle_id)
        groups.setdefault(key, []).append(item)

    return list(groups.values())


def split_articles_and_services(
    group: list[OrderItem],
) -> tuple[list[OrderItem], list[OrderItem]]:
    """Split a bundle into (articles, services) by StockLimitation."""
    articles = [i for i in group if i.stock_limitation in STOCK_LIMITATION_ARTICLE]
    services = [i for i in group if i.stock_limitation == STOCK_LIMITATION_SERVICE]
    return articles, services
