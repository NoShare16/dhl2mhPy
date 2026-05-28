import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dhl2mh.filter import filter_orders
from dhl2mh.mapper import map_order
from dhl2mh.models import Address, OrderItem, PlentyOrder

FIXTURE = Path(__file__).parent / "fixtures" / "plenty_order_bundle.json"


def _order(
    *,
    order_id: int = 1,
    type_id: int = 1,
    package_number: str | None = None,
    items: list[OrderItem] | None = None,
    addresses: list[Address] | None = None,
) -> PlentyOrder:
    return PlentyOrder(
        id=order_id,
        status_id=6.1,
        type_id=type_id,
        order_date=datetime(2025, 1, 15),
        package_number=package_number,
        order_items=items or [],
        addresses=addresses or [],
    )


def _article(item_id: int, *, bundle_id: str | None = None, weight_g: int | None = 1000) -> OrderItem:
    return OrderItem(
        id=item_id,
        bundle_id=bundle_id,
        stock_limitation=0,
        weight_g=Decimal(weight_g) if weight_g is not None else None,
    )


def _service(item_id: int, *, bundle_id: str | None = None) -> OrderItem:
    return OrderItem(id=item_id, bundle_id=bundle_id, stock_limitation=2)


def _addr(first: str = "Max", last: str = "Mustermann") -> Address:
    return Address(id=1, first_name=first, last_name=last)


# ── pass-through cases ────────────────────────────────────────────────────


def test_clean_order_with_single_article_passes():
    order = _order(items=[_article(1)])
    result = filter_orders([order])
    assert result.passed == [order]
    assert result.skipped == []


def test_bundle_with_article_and_services_passes():
    items = [
        _article(1, bundle_id="X"),
        _service(2, bundle_id="X"),
        _service(3, bundle_id="X"),
    ]
    order = _order(items=items)
    result = filter_orders([order])
    assert result.passed == [order]


def test_multiple_standalone_articles_pass():
    items = [_article(1), _article(2), _article(3)]
    order = _order(items=items)
    result = filter_orders([order])
    assert result.passed == [order]


def test_real_fixture_order_passes(monkeypatch):
    api = json.loads(FIXTURE.read_text())
    from dhl2mh.models import ApiOrder

    order = map_order(ApiOrder.model_validate(api), {1: "DE"})
    result = filter_orders([order])
    assert result.passed == [order]
    assert result.skipped == []


# ── skip reasons ──────────────────────────────────────────────────────────


def test_skip_when_package_number_already_set():
    order = _order(package_number="DHL-123", items=[_article(1)])
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "PackageNumber vorhanden" in result.skipped[0].reason
    assert "DHL-123" in result.skipped[0].reason


def test_empty_string_package_number_is_not_skipped():
    """Plenty sometimes returns "" — that's "not set", same as None."""
    order = _order(package_number="", items=[_article(1)])
    result = filter_orders([order])
    assert result.passed == [order]


def test_skip_when_type_id_not_1():
    order = _order(type_id=4, items=[_article(1)])
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "TypeId: 4" in result.skipped[0].reason


def test_skip_when_bundle_has_service_but_no_article():
    items = [_service(1, bundle_id="X"), _service(2, bundle_id="X")]
    order = _order(items=items)
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "Service-Bundle ohne Artikel" in result.skipped[0].reason


def test_skip_when_bundle_has_more_than_one_article():
    items = [
        _article(1, bundle_id="X"),
        _article(2, bundle_id="X"),
        _service(3, bundle_id="X"),
    ]
    order = _order(items=items)
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "Bundle 'X'" in result.skipped[0].reason
    assert "mehrere Artikel" in result.skipped[0].reason


def test_skip_when_no_articles_at_all():
    """e.g. all items had StockLimitation outside (0,1,2) — ignored as non-article/non-service."""
    item = OrderItem(id=1, stock_limitation=5)  # foreign stock_limitation
    order = _order(items=[item])
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "Keine Artikel" in result.skipped[0].reason


def test_skip_when_article_has_no_weight():
    items = [_article(1, weight_g=None)]
    order = _order(items=items)
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "ohne Gewichtsangabe" in result.skipped[0].reason
    assert "1" in result.skipped[0].reason


def test_skip_when_article_weight_is_zero():
    items = [_article(1, weight_g=0)]
    order = _order(items=items)
    result = filter_orders([order])
    assert len(result.skipped) == 1
    assert "ohne Gewichtsangabe" in result.skipped[0].reason


def test_services_without_weight_dont_trigger_skip():
    """Only articles need a weight. Services may have weight_g=None."""
    items = [_article(1, weight_g=2000, bundle_id="X"), _service(2, bundle_id="X")]
    order = _order(items=items)
    result = filter_orders([order])
    assert result.passed == [order]


# ── skipped-order metadata ────────────────────────────────────────────────


def test_skipped_order_captures_customer_name_and_item_count():
    items = [_article(1, weight_g=None), _article(2, weight_g=None)]
    order = _order(
        order_id=99,
        items=items,
        addresses=[_addr("Anna", "Schmidt")],
    )
    [skipped] = filter_orders([order]).skipped
    assert skipped.order_id == 99
    assert skipped.customer_name == "Anna Schmidt"
    assert skipped.item_count == 2


def test_skipped_order_uses_fallback_name_when_no_address():
    items = [_article(1, weight_g=None)]
    order = _order(items=items, addresses=[])
    [skipped] = filter_orders([order]).skipped
    assert skipped.customer_name == "N/A"


# ── ShopwareId removed ────────────────────────────────────────────────────


def test_missing_shopware_id_does_not_cause_skip():
    """C# original skipped on empty ShopwareId — that check has been removed."""
    order = PlentyOrder(
        id=1,
        status_id=6.1,
        type_id=1,
        order_date=datetime(2025, 1, 15),
        order_items=[_article(1)],
        shopware_id=None,  # absent
    )
    result = filter_orders([order])
    assert result.passed == [order]
