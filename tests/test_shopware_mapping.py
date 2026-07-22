import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dhl2mh.mapping import COLOR_GROUP_ID, SERVICE_AG, SERVICE_ISEK, SERVICE_ISEK_KG
from dhl2mh.models import (
    OrderItem,
    PlentyOrder,
    SwLineItemPayload,
    SwOrder,
    SwOrderLineItem,
    SwProduct,
    SwProductInfo,
    SwPropertyOption,
)
from dhl2mh.shopware_mapping import (
    assign_former_parent_ids,
    assign_water_connection,
    product_display_name,
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

    result = assign_former_parent_ids(order, _sw_order())

    assert (result.matched, result.split) == (3, 0)
    assert all(it.former_parent_id == FORMER_PARENT for it in order.order_items)


def test_unmatched_positions_keep_none():
    order = _order(OrderItem(id=771883), OrderItem(id=999999))

    result = assign_former_parent_ids(order, _sw_order())

    assert result.matched == 1
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

    result = assign_former_parent_ids(order, empty)

    assert (result.matched, result.split) == (0, 0)
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


# ── one service for two articles → one Plenty position, two parents ─────────
#
# Modelled on order 238574 / MK90156: one Altgerätemitnahme per fridge arrives as
# two Shopware line items of quantity 1 sharing productNumber 783116, which
# Plenty books as a single position of quantity 2.

PARENT_A = "019f4115c9a97017b6334697aa80ecfe"
PARENT_B = "019f4115c9a97017b6334697ad6e3e1b"


def _sw_line(product_number: str, former_parent_id: str, quantity: int = 1):
    return SwOrderLineItem(
        type="dvsn-product-option",
        quantity=Decimal(quantity),
        payload=SwLineItemPayload(
            product_number=product_number,
            dvsn_product_option_former_parent_id=former_parent_id,
        ),
    )


def _two_fridge_order() -> SwOrder:
    return SwOrder(
        order_number="MK90156",
        line_items=[
            _sw_line("784632", PARENT_A),  # Kühlschrank
            _sw_line("783116", PARENT_A),  # Altgerätemitnahme
            _sw_line("784642", PARENT_B),  # Gefrierschrank
            _sw_line("783116", PARENT_B),  # Altgerätemitnahme
        ],
    )


def test_service_for_two_articles_is_split_per_parent():
    order = _order(
        OrderItem(id=784632, stock_limitation=1),
        OrderItem(id=784642, stock_limitation=1),
        OrderItem(id=783116, stock_limitation=2, quantity=Decimal(2), packages=Decimal(2)),
    )

    result = assign_former_parent_ids(order, _two_fridge_order())

    assert (result.matched, result.split) == (3, 1)
    services = [it for it in order.order_items if it.id == 783116]
    assert len(services) == 2
    assert [s.former_parent_id for s in services] == [PARENT_A, PARENT_B]
    # every article keeps its own service — this is the bug that shipped orders
    # with one article silently missing its Altgerätemitnahme
    assert {it.former_parent_id for it in order.order_items} == {PARENT_A, PARENT_B}


def test_split_positions_take_their_quantity_from_shopware():
    order = _order(
        OrderItem(id=783116, stock_limitation=2, quantity=Decimal(3), packages=Decimal(3))
    )
    sw = SwOrder(
        order_number="X",
        line_items=[_sw_line("783116", PARENT_A, 1), _sw_line("783116", PARENT_B, 2)],
    )

    assign_former_parent_ids(order, sw)

    assert [(it.former_parent_id, it.quantity, it.packages) for it in order.order_items] == [
        (PARENT_A, Decimal(1), Decimal(1)),
        (PARENT_B, Decimal(2), Decimal(2)),
    ]


def test_repeated_line_items_with_one_parent_are_not_split():
    """Two line items, same parent → still a single position (quantity untouched)."""
    order = _order(OrderItem(id=783116, stock_limitation=2, quantity=Decimal(2)))
    sw = SwOrder(
        order_number="X",
        line_items=[_sw_line("783116", PARENT_A), _sw_line("783116", PARENT_A)],
    )

    result = assign_former_parent_ids(order, sw)

    assert (result.matched, result.split) == (1, 0)
    assert len(order.order_items) == 1
    assert order.order_items[0].quantity == Decimal(2)


def test_split_positions_do_not_share_mutable_state():
    order = _order(OrderItem(id=783116, stock_limitation=2, quantity=Decimal(2)))
    sw = SwOrder(
        order_number="X",
        line_items=[_sw_line("783116", PARENT_A), _sw_line("783116", PARENT_B)],
    )

    assign_former_parent_ids(order, sw)
    first, second = order.order_items
    first.service_match_codes.append("AG")

    assert second.service_match_codes == []


def test_splitting_preserves_position_order():
    """A split position stays where it was, so the XML item order is stable."""
    order = _order(
        OrderItem(id=784632, stock_limitation=1),
        OrderItem(id=783116, stock_limitation=2, quantity=Decimal(2)),
        OrderItem(id=784642, stock_limitation=1),
    )

    assign_former_parent_ids(order, _two_fridge_order())

    assert [it.id for it in order.order_items] == [784632, 783116, 783116, 784642]


# ── Plenty variation id ≠ Shopware productNumber (Installationsservice KG) ──


def test_isek_kg_matches_via_shopware_product_number_alias():
    """Plenty books variation 783172, Shopware sends productNumber 783149."""
    order = _order(
        OrderItem(id=784632, stock_limitation=1),
        OrderItem(id=SERVICE_ISEK_KG, stock_limitation=2, quantity=Decimal(2)),
    )
    sw = SwOrder(
        order_number="X",
        line_items=[
            _sw_line("784632", PARENT_A),
            _sw_line(str(SERVICE_ISEK), PARENT_A),
            _sw_line(str(SERVICE_ISEK), PARENT_B),
        ],
    )

    result = assign_former_parent_ids(order, sw)

    assert result.split == 1
    services = [it for it in order.order_items if it.id == SERVICE_ISEK_KG]
    assert [s.former_parent_id for s in services] == [PARENT_A, PARENT_B]


def test_exact_product_number_wins_over_alias():
    """A line item carrying 783172 itself must not be overridden by the alias."""
    order = _order(OrderItem(id=SERVICE_ISEK_KG, stock_limitation=2))
    sw = SwOrder(
        order_number="X",
        line_items=[
            _sw_line(str(SERVICE_ISEK_KG), PARENT_A),
            _sw_line(str(SERVICE_ISEK), PARENT_B),
        ],
    )

    assign_former_parent_ids(order, sw)

    assert len(order.order_items) == 1
    assert order.order_items[0].former_parent_id == PARENT_A


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


# ── product_display_name: ProductName from manufacturerNumber + color ────────


def _product_info(*, manufacturer=None, color=None, color_group=COLOR_GROUP_ID):
    props = []
    if color is not None:
        props.append(SwPropertyOption(name=color, group_id=color_group))
    return SwProductInfo(manufacturer_number=manufacturer, properties=props)


def test_product_name_combines_manufacturer_and_color():
    info = _product_info(manufacturer="HE517ABW0", color="Schwarz")
    assert product_display_name(info, fallback="Plenty-Name") == "HE517ABW0 Schwarz"


def test_product_name_falls_back_when_manufacturer_missing():
    info = _product_info(manufacturer=None, color="Schwarz")
    assert product_display_name(info, fallback="Plenty-Name") == "Plenty-Name"


def test_product_name_falls_back_when_color_missing():
    info = _product_info(manufacturer="HE517ABW0", color=None)
    assert product_display_name(info, fallback="Plenty-Name") == "Plenty-Name"


def test_product_name_ignores_color_from_other_group():
    # A property in a different group must not be treated as the color.
    info = _product_info(manufacturer="HE517ABW0", color="Elektro", color_group="other")
    assert product_display_name(info, fallback="Plenty-Name") == "Plenty-Name"
    assert info.color(COLOR_GROUP_ID) is None
