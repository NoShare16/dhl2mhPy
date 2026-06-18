import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dhl2mh.mapper import map_order
from dhl2mh.models import ApiOrder, ApiOrderPage

FIXTURE = Path(__file__).parent / "fixtures" / "plenty_order_bundle.json"
ORDERS_FIXTURE = Path(__file__).parent / "fixtures" / "plenty_orders.json"
ARTICLE_BUNDLE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "plenty_order_mit_bundle_artikel.json"
)
COUNTRIES = {1: "DE", 2: "AT"}


@pytest.fixture
def real_order() -> ApiOrder:
    return ApiOrder.model_validate(json.loads(FIXTURE.read_text()))


@pytest.fixture
def order_783117() -> ApiOrder:
    """Real order MK89576 (id 237553): article + AG service + the 783117 set
    (bundle parent typeId 2) with its components 783143/783147/783148 (typeId 3)."""
    page = ApiOrderPage.model_validate(json.loads(ORDERS_FIXTURE.read_text()))
    return next(o for o in page.entries if o.id == 237553)


def test_real_order_top_level_fields(real_order):
    o = map_order(real_order, COUNTRIES)
    assert o.id == 235655
    assert o.status_id == 6.1
    assert o.type_id == 1
    assert isinstance(o.order_date, datetime)


def test_real_order_filters_non_article_items(real_order):
    """Item 0 is Shipping Costs (typeId=6) — must be dropped."""
    o = map_order(real_order, COUNTRIES)
    assert len(o.order_items) == 3
    assert all(name != "Shipping Costs" for name in (i.name for i in o.order_items))


def test_real_order_bundle_id_extracted_from_property_1021(real_order):
    o = map_order(real_order, COUNTRIES)
    by_name = {i.name: i for i in o.order_items}

    capa = by_name["Gutmann Deckenmodul Capa 07 EM"]
    install = by_name["Installationsservice DHL 2MH - BMD"]
    dishwasher = by_name["Geschirrspüler SMI69U85EU"]

    assert capa.bundle_id == "1234"
    assert install.bundle_id == "1234"
    assert dishwasher.bundle_id is None  # standalone article, no bundle


def test_real_order_former_parent_id_seeded_from_property_1021(real_order):
    """former_parent_id starts as the Plenty 1021 value (Shopware overwrites later)."""
    o = map_order(real_order, COUNTRIES)
    by_name = {i.name: i for i in o.order_items}
    assert by_name["Gutmann Deckenmodul Capa 07 EM"].former_parent_id == "1234"
    assert by_name["Installationsservice DHL 2MH - BMD"].former_parent_id == "1234"
    assert by_name["Geschirrspüler SMI69U85EU"].former_parent_id is None


def test_real_order_stock_limitation_classifies_article_vs_service(real_order):
    o = map_order(real_order, COUNTRIES)
    by_name = {i.name: i for i in o.order_items}
    assert by_name["Gutmann Deckenmodul Capa 07 EM"].stock_limitation == 0
    assert by_name["Installationsservice DHL 2MH - BMD"].stock_limitation == 2
    assert by_name["Geschirrspüler SMI69U85EU"].stock_limitation == 0


def test_real_order_variation_measurements_mapped(real_order):
    o = map_order(real_order, COUNTRIES)
    by_name = {i.name: i for i in o.order_items}
    capa = by_name["Gutmann Deckenmodul Capa 07 EM"]
    # weight_g in fixture variation for capa
    assert capa.weight_g is not None
    assert capa.weight_g > 0
    assert capa.height_mm > 0 or capa.length_mm > 0 or capa.width_mm > 0


def test_real_order_delivery_address_picked_via_relation_type_2(real_order):
    """addressRelations: typeId=1=billing, typeId=2=delivery — must use 2."""
    o = map_order(real_order, COUNTRIES)
    assert len(o.addresses) == 1
    addr = o.addresses[0]
    assert addr.id == 338677  # the typeId=2 address from the fixture
    assert addr.first_name == "Alexander"
    assert addr.last_name == "Wirschke"
    assert addr.postal_code == "31180"
    assert addr.country_code == "DE"
    assert addr.email == "aw@mykitchens.de"
    assert addr.phone_number is None  # no typeId=4 option present in this fixture


def test_real_order_customer_id_from_receiver_relation(real_order):
    o = map_order(real_order, COUNTRIES)
    assert o.addresses[0].customer_id == 4043184


def test_real_order_shopware_id_from_order_property_7(real_order):
    o = map_order(real_order, COUNTRIES)
    assert o.shopware_id == "ML19516"


def test_real_order_package_number_none_when_only_empty_package(real_order):
    o = map_order(real_order, COUNTRIES)
    # Fixture's single shipping package has packageNumber="" → treated as "no number"
    assert o.package_number is None


# ── 783117 bundle set (parent kept, components dropped) ─────────────────────


def test_bundle_parent_783117_is_kept(order_783117):
    """The set service 783117 arrives as a bundle parent (typeId 2) and must be
    read — it's the position carrying the AWS+DPW MatchCodes."""
    o = map_order(order_783117, COUNTRIES)
    ids = {i.id for i in o.order_items}
    assert 783117 in ids
    parent = next(i for i in o.order_items if i.id == 783117)
    assert parent.stock_limitation == 2


def test_bundle_components_783143_147_148_are_dropped(order_783117):
    """The set's fulfilment components (typeId 3) must not become services —
    otherwise they'd emit duplicate/extra MatchCodes alongside the parent."""
    o = map_order(order_783117, COUNTRIES)
    ids = {i.id for i in o.order_items}
    assert ids.isdisjoint({783143, 783147, 783148})


def test_bundle_order_keeps_article_and_standalone_service(order_783117):
    o = map_order(order_783117, COUNTRIES)
    ids = {i.id for i in o.order_items}
    assert 784144 in ids  # the article
    assert 783116 in ids  # standalone AG service (typeId 1)
    assert 0 not in ids  # shipping costs (typeId 6) dropped


def test_service_bundle_parent_flagged_is_bundle_parent(order_783117):
    o = map_order(order_783117, COUNTRIES)
    parent = next(i for i in o.order_items if i.id == 783117)
    assert parent.is_bundle_parent is True
    # a normal position is not a bundle parent
    assert next(i for i in o.order_items if i.id == 784144).is_bundle_parent is False


def test_article_bundle_parent_kept_and_flagged():
    """Article bundle (Quooker): parent 778101 is a bundle parent (typeId 2),
    components (typeId 3) are dropped. Detection of the unsupported article
    bundle happens later in the filter via is_bundle_parent + stock_limitation."""
    api = ApiOrder.model_validate(json.loads(ARTICLE_BUNDLE_FIXTURE.read_text()))
    o = map_order(api, COUNTRIES)
    ids = {i.id for i in o.order_items}
    assert 778101 in ids
    assert ids.isdisjoint({783103, 785301, 785307})  # components dropped
    assert next(i for i in o.order_items if i.id == 778101).is_bundle_parent is True


# ── edge cases ──────────────────────────────────────────────────────────────


def _minimal_order(**overrides) -> dict:
    base = {
        "id": 1,
        "statusId": 6.1,
        "typeId": 1,
        "createdAt": "2025-01-15T10:00:00+00:00",
        "orderItems": [],
        "addresses": [],
        "addressRelations": [],
        "relations": [],
        "properties": [],
        "shippingPackages": [],
    }
    base.update(overrides)
    return base


def test_no_delivery_relation_yields_empty_addresses():
    api = ApiOrder.model_validate(_minimal_order())
    o = map_order(api, COUNTRIES)
    assert o.addresses == []


def test_unknown_country_id_falls_back_to_fehler():
    api = ApiOrder.model_validate(
        _minimal_order(
            addressRelations=[{"typeId": 2, "addressId": 99}],
            addresses=[{"id": 99, "name2": "X", "name3": "Y", "countryId": 999}],
        )
    )
    o = map_order(api, COUNTRIES)
    assert o.addresses[0].country_code == "FEHLER"


def test_missing_shipping_packages_yields_none():
    api = ApiOrder.model_validate(_minimal_order())
    o = map_order(api, COUNTRIES)
    assert o.package_number is None


def test_package_number_taken_from_later_non_empty_package():
    """Plenty's original package [0] is empty; the tracking number sits on a
    later package — the mapper must find it so the filter skips shipped orders."""
    api = ApiOrder.model_validate(
        _minimal_order(
            shippingPackages=[
                {"packageNumber": ""},
                {"packageNumber": "680214534025"},
            ]
        )
    )
    o = map_order(api, COUNTRIES)
    assert o.package_number == "680214534025"


def test_missing_shopware_property_yields_none():
    api = ApiOrder.model_validate(_minimal_order())
    o = map_order(api, COUNTRIES)
    assert o.shopware_id is None


def test_item_without_variation_keeps_zero_measurements():
    api = ApiOrder.model_validate(
        _minimal_order(
            orderItems=[
                {
                    "typeId": 1,
                    "itemVariationId": 42,
                    "orderItemName": "Bare",
                    "quantity": 1,
                }
            ]
        )
    )
    o = map_order(api, COUNTRIES)
    assert o.order_items[0].weight_g is None
    assert o.order_items[0].height_mm == 0
    assert o.order_items[0].stock_limitation == 0


def test_item_without_bundle_property_yields_none():
    api = ApiOrder.model_validate(
        _minimal_order(
            orderItems=[
                {
                    "typeId": 1,
                    "itemVariationId": 42,
                    "orderItemName": "Standalone",
                    "quantity": 1,
                    "properties": [{"typeId": 99, "value": "irrelevant"}],
                }
            ]
        )
    )
    o = map_order(api, COUNTRIES)
    assert o.order_items[0].bundle_id is None


def test_quantity_propagates_to_packages_field():
    api = ApiOrder.model_validate(
        _minimal_order(
            orderItems=[
                {
                    "typeId": 1,
                    "itemVariationId": 42,
                    "orderItemName": "X",
                    "quantity": 3,
                }
            ]
        )
    )
    o = map_order(api, COUNTRIES)
    assert o.order_items[0].quantity == Decimal(3)
    assert o.order_items[0].packages == Decimal(3)
