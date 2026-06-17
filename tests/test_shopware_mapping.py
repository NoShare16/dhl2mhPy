import json
from datetime import datetime
from pathlib import Path

from dhl2mh.mapping import SERVICE_AG
from dhl2mh.models import (
    OrderItem,
    PlentyOrder,
    SwLineItemPayload,
    SwOrder,
    SwOrderLineItem,
    SwProduct,
    SwPropertyOption,
)
from dhl2mh.shopware_mapping import (
    assign_former_parent_ids,
    assign_water_connection,
    require_service_former_parent_ids,
)

SW_ORDER_FIXTURE = Path(__file__).parent / "fixtures" / "sw_order_mit_accept.json"
SW_ORDER_EXTENDED_FIXTURE = Path(__file__).parent / "fixtures" / "sw_order_erweitert.json"
WATER_GROUP_ID = "8910dbddf00a4d94998289840033982d"

FORMER_PARENT = "019ed4680e07739a8bda655a837f5cc2"


def _order(*items: OrderItem) -> PlentyOrder:
    return PlentyOrder(
        id=1,
        status_id=6.1,
        type_id=1,
        order_date=datetime(2026, 6, 17),
        order_items=list(items),
    )


def _sw_order() -> SwOrder:
    return SwOrder.model_validate(json.loads(SW_ORDER_FIXTURE.read_text())["data"][0])


def test_assigns_former_parent_id_to_matching_positions():
    order = _order(
        OrderItem(id=771883),  # the article
        OrderItem(id=783116),  # service: Altgerätemitnahme
        OrderItem(id=783140),  # service: Installationsservice
    )

    matched = assign_former_parent_ids(order, _sw_order())

    assert matched == 3
    assert all(it.former_parent_id == FORMER_PARENT for it in order.order_items)


def test_unmatched_positions_keep_none():
    order = _order(OrderItem(id=771883), OrderItem(id=999999))

    matched = assign_former_parent_ids(order, _sw_order())

    assert matched == 1
    by_id = {it.id: it for it in order.order_items}
    assert by_id[771883].former_parent_id == FORMER_PARENT
    assert by_id[999999].former_parent_id is None


def test_promotion_line_items_without_product_number_are_ignored():
    """Promotions have no productNumber/formerParentId — they must not match."""
    sw = _sw_order()
    promo_count = sum(1 for li in sw.line_items if li.type == "promotion")
    assert promo_count == 2  # guards the fixture

    order = _order(OrderItem(id=771883))
    assign_former_parent_ids(order, sw)

    assert order.order_items[0].former_parent_id == FORMER_PARENT


def test_no_line_items_assigns_nothing():
    order = _order(OrderItem(id=771883))
    empty = SwOrder(order_number="MK89643", line_items=[])

    matched = assign_former_parent_ids(order, empty)

    assert matched == 0
    assert order.order_items[0].former_parent_id is None


# ── precedence: Shopware overwrites Plenty, but only when present ────────────


def _sw_with(product_number: str, former_parent_id: str | None) -> SwOrder:
    return SwOrder(
        order_number="X",
        line_items=[
            SwOrderLineItem(
                type="product",
                payload=SwLineItemPayload(
                    product_number=product_number,
                    dvsn_product_option_former_parent_id=former_parent_id,
                ),
            )
        ],
    )


def test_shopware_value_overwrites_plenty_seed():
    order = _order(OrderItem(id=771883, former_parent_id="plenty-1234"))
    assign_former_parent_ids(order, _sw_with("771883", "sw-uuid"))
    assert order.order_items[0].former_parent_id == "sw-uuid"


def test_empty_shopware_value_does_not_clear_plenty_seed():
    order = _order(OrderItem(id=771883, former_parent_id="plenty-1234"))
    assign_former_parent_ids(order, _sw_with("771883", ""))
    assert order.order_items[0].former_parent_id == "plenty-1234"


# ── water connection (Festwasser) extraction ────────────────────────────────


def _sw_product_order(product_number: str, properties: list[SwPropertyOption]) -> SwOrder:
    return SwOrder(
        order_number="X",
        line_items=[
            SwOrderLineItem(
                type="product",
                product=SwProduct(product_number=product_number, properties=properties),
            )
        ],
    )


def test_water_connection_yes_sets_festwasser_true():
    order = _order(OrderItem(id=771883))
    sw = _sw_product_order(
        "771883", [SwPropertyOption(name="ja", group_id=WATER_GROUP_ID)]
    )
    matched = assign_water_connection(order, sw)

    assert matched == 1
    assert order.order_items[0].festwasser is True


def test_water_connection_no_sets_festwasser_false():
    order = _order(OrderItem(id=771883, festwasser=True))  # ensure it gets reset
    sw = _sw_product_order(
        "771883", [SwPropertyOption(name="nein", group_id=WATER_GROUP_ID)]
    )
    assign_water_connection(order, sw)

    assert order.order_items[0].festwasser is False


def test_missing_water_property_leaves_festwasser_untouched():
    order = _order(OrderItem(id=771883))
    sw = _sw_product_order(
        "771883", [SwPropertyOption(name="Weiß", group_id="some-other-group")]
    )
    matched = assign_water_connection(order, sw)

    assert matched == 0
    assert order.order_items[0].festwasser is False


def test_water_connection_from_extended_fixture_is_false_for_nein():
    """Real extended fixture: the Smeg Herd has Wasseranschluss = 'nein'."""
    sw = SwOrder.model_validate(
        json.loads(SW_ORDER_EXTENDED_FIXTURE.read_text())["data"][0]
    )
    order = _order(OrderItem(id=771883))
    assign_water_connection(order, sw)

    assert order.order_items[0].festwasser is False


# ── skip stage: former_parent_id mandatory on service positions ─────────────


def test_skips_order_when_service_lacks_former_parent_id():
    order = _order(
        OrderItem(id=1, stock_limitation=0, former_parent_id="x"),  # article
        OrderItem(id=SERVICE_AG, stock_limitation=2, former_parent_id=None),  # service
    )
    result = require_service_former_parent_ids([order])

    assert result.passed == []
    assert len(result.skipped) == 1
    assert "FormerParentId" in result.skipped[0].reason


def test_passes_order_when_all_services_have_former_parent_id():
    order = _order(
        OrderItem(id=1, stock_limitation=0, former_parent_id="x"),
        OrderItem(id=SERVICE_AG, stock_limitation=2, former_parent_id="x"),
    )
    result = require_service_former_parent_ids([order])

    assert result.passed == [order]
    assert result.skipped == []


def test_article_only_order_without_former_parent_is_not_skipped():
    """No services → the field isn't required."""
    order = _order(OrderItem(id=1, stock_limitation=0, former_parent_id=None))
    result = require_service_former_parent_ids([order])

    assert result.passed == [order]
    assert result.skipped == []


def test_discount_position_without_former_parent_does_not_skip():
    """A StockLimitation-2 discount (non-whitelisted id) is not a service."""
    order = _order(
        OrderItem(id=1, stock_limitation=0, former_parent_id="x"),
        OrderItem(id=787119, stock_limitation=2, former_parent_id=None),  # "Rabatt"
    )
    result = require_service_former_parent_ids([order])

    assert result.passed == [order]
    assert result.skipped == []
