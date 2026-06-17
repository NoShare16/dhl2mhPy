from decimal import Decimal

from dhl2mh.bundles import group_by_bundle, split_articles_and_services
from dhl2mh.models import OrderItem


def _item(
    item_id: int,
    *,
    bundle_id: str | None = None,
    former_parent_id: str | None = None,
    stock: int = 0,
) -> OrderItem:
    return OrderItem(
        id=item_id,
        bundle_id=bundle_id,
        former_parent_id=former_parent_id,
        stock_limitation=stock,
        weight_g=Decimal(1000),
    )


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


def test_former_parent_id_seeds_from_bundle_id_when_not_overridden():
    """No Shopware value → grouping falls back to the Plenty bundle id."""
    a = _item(1, bundle_id="X")
    b = _item(2, bundle_id="X", stock=2)
    assert a.former_parent_id == "X"
    assert group_by_bundle([a, b]) == [[a, b]]


def test_grouping_follows_former_parent_id_over_bundle_id():
    """Shopware override regroups: same bundle_id but different former_parent_id."""
    a = _item(1, bundle_id="X", former_parent_id="U1")
    b = _item(2, bundle_id="X", former_parent_id="U2", stock=2)
    assert group_by_bundle([a, b]) == [[a], [b]]


def test_different_bundle_id_same_former_parent_id_groups_together():
    a = _item(1, bundle_id="X", former_parent_id="U")
    b = _item(2, bundle_id="Y", former_parent_id="U", stock=2)
    assert group_by_bundle([a, b]) == [[a, b]]


def test_split_articles_and_services_by_stock_limitation():
    art1 = _item(1, stock=0)
    art2 = _item(2, stock=1)
    svc = _item(3, stock=2)
    other = _item(4, stock=5)  # neither article nor service — ignored

    articles, services = split_articles_and_services([art1, art2, svc, other])
    assert articles == [art1, art2]
    assert services == [svc]
