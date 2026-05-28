from decimal import Decimal

from dhl2mh.bundles import group_by_bundle, split_articles_and_services
from dhl2mh.models import OrderItem


def _item(item_id: int, *, bundle_id: str | None = None, stock: int = 0) -> OrderItem:
    return OrderItem(id=item_id, bundle_id=bundle_id, stock_limitation=stock, weight_g=Decimal(1000))


def test_items_sharing_bundle_id_form_one_group():
    a = _item(1, bundle_id="X")
    b = _item(2, bundle_id="X", stock=2)
    groups = group_by_bundle([a, b])
    assert groups == [[a, b]]


def test_items_without_bundle_id_each_form_own_group():
    a = _item(1)
    b = _item(2)
    groups = group_by_bundle([a, b])
    assert groups == [[a], [b]]


def test_mixed_bundles_and_standalones_preserve_first_seen_order():
    a = _item(1, bundle_id="X")
    standalone = _item(2)
    b = _item(3, bundle_id="X", stock=2)
    c = _item(4, bundle_id="Y")
    groups = group_by_bundle([a, standalone, b, c])
    # bundle X (a, b — even though 'standalone' was between them in input),
    # then standalone, then bundle Y. Order is "first-seen".
    assert groups == [[a, b], [standalone], [c]]


def test_split_articles_and_services_by_stock_limitation():
    art1 = _item(1, stock=0)
    art2 = _item(2, stock=1)
    svc = _item(3, stock=2)
    other = _item(4, stock=5)  # neither article nor service — ignored

    articles, services = split_articles_and_services([art1, art2, svc, other])
    assert articles == [art1, art2]
    assert services == [svc]
