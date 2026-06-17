import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dhl2mh.mapper import map_order
from dhl2mh.models import ApiOrder

FIXTURE = Path(__file__).parent / "fixtures" / "plenty_order_bundle.json"
COUNTRIES = {1: "DE", 2: "AT"}


@pytest.fixture
def real_order() -> ApiOrder:
    return ApiOrder.model_validate(json.loads(FIXTURE.read_text()))


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


def test_real_order_package_number_taken_from_first_shipping_package(real_order):
    o = map_order(real_order, COUNTRIES)
    # Fixture has one shipping package with packageNumber="" — empty string, not None
    assert o.package_number == ""


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
