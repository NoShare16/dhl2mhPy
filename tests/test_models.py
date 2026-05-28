from datetime import datetime
from decimal import Decimal

from dhl2mh.models import (
    ApiCountry,
    ApiOrderPage,
    OrderItem,
    PlentyOrder,
    SkippedOrder,
)


def test_api_order_page_parses_plenty_camel_case():
    page = ApiOrderPage.model_validate(
        {
            "isLastPage": False,
            "entries": [
                {
                    "id": 12345,
                    "statusId": 6.1,
                    "typeId": 1,
                    "createdAt": "2025-01-15T10:30:00+00:00",
                    "orderItems": [
                        {
                            "typeId": 1,
                            "itemVariationId": 789,
                            "orderItemName": "Sofa",
                            "quantity": 1,
                            "variation": {
                                "stockLimitation": 0,
                                "weightG": 50000,
                                "widthMM": 2000,
                                "lengthMM": 1000,
                                "heightMM": 800,
                            },
                        }
                    ],
                    "shippingPackages": [{"packageNumber": None}],
                }
            ],
        }
    )

    assert page.is_last_page is False
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.id == 12345
    assert entry.status_id == 6.1
    item = entry.order_items[0]
    assert item.item_variation_id == 789
    assert item.variation is not None
    assert item.variation.weight_g == 50000
    assert item.variation.width_mm == 2000


def test_country_alias_handles_iso_code_2():
    c = ApiCountry.model_validate({"id": 1, "isoCode2": "DE"})
    assert c.iso_code2 == "DE"


def test_unknown_api_fields_are_ignored():
    """Plenty may add fields we don't model — must not blow up."""
    page = ApiOrderPage.model_validate(
        {
            "isLastPage": True,
            "entries": [],
            "page": 1,
            "totalsCount": 0,
            "futureField": "whatever",
        }
    )
    assert page.entries == []


def test_domain_order_constructs_with_defaults():
    order = PlentyOrder(
        id=1,
        status_id=6.1,
        type_id=1,
        order_date=datetime(2025, 1, 15),
    )
    assert order.order_items == []
    assert order.addresses == []
    assert order.package_number is None


def test_order_item_holds_filter_stage_fields():
    item = OrderItem(
        id=42,
        service_ids=[783152],
        service_match_codes=["SWG"],
        categories=["cat-a"],
        weight_kg=Decimal("125.50"),
    )
    assert item.service_match_codes == ["SWG"]
    assert item.weight_kg == Decimal("125.50")


def test_skipped_order_carries_reason():
    s = SkippedOrder(
        order_id=99,
        order_date=datetime(2025, 1, 15),
        reason="Keine ShopwareID vorhanden",
        customer_name="Max Mustermann",
        item_count=3,
    )
    assert s.reason.startswith("Keine ShopwareID")
