import re
from datetime import datetime
from decimal import Decimal

import pytest
from lxml import etree

from dhl2mh.models import Address, OrderItem, PlentyOrder
from dhl2mh.xml_builder import DSI_NS, XSI_NS, OrderXmlBuilder

DSI = f"{{{DSI_NS}}}"
XSI = f"{{{XSI_NS}}}"


def _addr(**overrides) -> Address:
    base = dict(
        id=1,
        customer_id=4042,
        first_name="Max",
        last_name="Mustermann",
        country_code="DE",
        postal_code="10115",
        city="Berlin",
        street="Hauptstr. 1",
        phone_number="030123",
        email="x@y.de",
    )
    base.update(overrides)
    return Address(**base)


def _article(**overrides) -> OrderItem:
    base = dict(
        id=1003,
        name="Capa 07",
        quantity=Decimal(1),
        packages=Decimal(1),
        stock_limitation=0,
        weight_kg=Decimal("37.00"),
        volume_cbm=Decimal("0.350"),
        service_match_codes=["AG"],
    )
    base.update(overrides)
    return OrderItem(**base)


def _order(**overrides) -> PlentyOrder:
    base = dict(
        id=12345,
        status_id=6.1,
        type_id=1,
        order_date=datetime(2025, 1, 15),
        addresses=[_addr()],
        order_items=[_article()],
    )
    base.update(overrides)
    return PlentyOrder(**base)


def _root(builder: OrderXmlBuilder | None = None, order: PlentyOrder | None = None):
    builder = builder or OrderXmlBuilder()
    order = order or _order()
    return etree.fromstring(builder.build(order))


# ── envelope ───────────────────────────────────────────────────────────────


def test_root_is_dsi_namespaced_transmission():
    root = _root()
    assert root.tag == f"{DSI}Transmission"
    assert root.nsmap.get("dsi") == DSI_NS


def test_xml_declaration_with_utf8_encoding():
    xml = OrderXmlBuilder().build(_order())
    assert xml.startswith(b"<?xml")
    head = xml[:80].lower()
    assert b"utf-8" in head


def test_transmission_creation_date_is_iso_with_offset():
    creation = _root().find("TransmissionCreationDate").text
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", creation)


def test_party_ids_use_constructor_args():
    root = _root(OrderXmlBuilder(sending_party_id="ACME", receiving_party_id="X"))
    assert root.find("SendingPartyID").text == "ACME"
    assert root.find("ReceivingPartyID").text == "X"


def test_message_envelope_default_values():
    root = _root()
    messages = root.find("Messages")
    assert messages.find("MessageStructureVersion").text == "2.23"
    assert messages.find("MessageControlNumber").text == "1"
    assert root.find("MessageCount").text == "1"


# ── order element ──────────────────────────────────────────────────────────


def test_order_element_under_message_content_carries_id_and_date():
    root = _root()
    order_el = root.find(f"Messages/MessageContent/{DSI}Order")
    assert order_el is not None
    assert order_el.find("OrderId/Id").text == "12345"
    assert order_el.find("OrderId/System").text == "HDE"
    assert order_el.find("OrderNr").text == "12345"
    assert order_el.find("OrderDate").text == "2025-01-15"
    assert order_el.find("OrderType").text == "LIEF_KK"
    assert order_el.find("ProductType").text == "ZH"
    assert order_el.find("FreightTerms").text == "FV"


# ── sender / receiver ──────────────────────────────────────────────────────


def test_sender_is_supplier_with_configured_partner_id():
    root = _root(OrderXmlBuilder(sender_partner_id="3", sending_party_id="HDE"))
    sender = root.find(f".//{DSI}Order/Sender")
    assert sender.get(f"{XSI}type") == "dsi:Supplier"
    assert sender.find("PartnerId/System").text == "HDE"
    assert sender.find("PartnerId/Id").text == "3"


def test_receiver_is_customer_with_customer_id_from_address():
    root = _root()
    receiver = root.find(f".//{DSI}Order/Receiver")
    assert receiver.get(f"{XSI}type") == "dsi:Customer"
    assert receiver.find("PartnerId/Id").text == "4042"
    assert receiver.find("Name").text == "Max Mustermann"


def test_receiver_address_fields_populated_from_domain_address():
    root = _root()
    addr = root.find(f".//{DSI}Order/Receiver/Address")
    assert addr.find("Name1").text == "Max Mustermann"
    assert addr.find("CountryCode").text == "DE"
    assert addr.find("PostalCode").text == "10115"
    assert addr.find("City").text == "Berlin"
    assert addr.find("Street").text == "Hauptstr. 1"
    assert addr.find("PhoneNumber1").text == "030123"
    assert addr.find("EMail").text == "x@y.de"


def test_missing_address_fields_become_empty_elements():
    """Blank fields must still appear in the XML (lxml self-closes them, which
    is XML-equivalent to <Tag></Tag> and accepted by DHL DeliverIT)."""
    bare = _addr(
        country_code=None, postal_code=None, city=None,
        street=None, phone_number=None, email=None,
    )
    root = _root(order=_order(addresses=[bare]))
    addr = root.find(f".//{DSI}Order/Receiver/Address")
    assert addr.find("CountryCode").text == "DE"  # default fallback
    # Empty fields exist but carry no text — equivalent to <PostalCode/>
    for tag in ("PostalCode", "City", "Street", "PhoneNumber1", "EMail"):
        el = addr.find(tag)
        assert el is not None, f"{tag} must exist"
        assert el.text in (None, ""), f"{tag} must be empty"


def test_order_without_address_raises():
    o = _order(addresses=[])
    with pytest.raises(ValueError, match="no delivery address"):
        OrderXmlBuilder().build(o)


# ── items ─────────────────────────────────────────────────────────────────


def test_item_carries_catalog_product_quantity_packages_volume_weight():
    root = _root()
    item = root.find(f".//{DSI}Order/Items")
    assert item.find("CatalogNr").text == "1003"
    assert item.find("ProductName").text == "Capa 07"
    assert item.find("Quantity").text == "1"
    assert item.find("Packages").text == "1"
    assert item.find("Volume/Amount").text == "0.350"
    assert item.find("Volume/Unit").text == "CBM"
    assert item.find("Weight/Amount").text == "37.00"
    assert item.find("Weight/Unit").text == "KG"


def test_only_articles_become_items_elements():
    """Service items (stock_limitation=2) are NOT emitted as <Items>."""
    article = _article(id=1, service_match_codes=["AG"])
    service = OrderItem(id=783116, name="AG-Service", stock_limitation=2)
    root = _root(order=_order(order_items=[article, service]))
    items = root.findall(f".//{DSI}Order/Items")
    assert len(items) == 1
    assert items[0].find("CatalogNr").text == "1"


def test_each_match_code_becomes_a_services_block():
    article = _article(service_match_codes=["AWS", "DPW", "VPR"])
    root = _root(order=_order(order_items=[article]))
    services = root.findall(f".//{DSI}Order/Items/Services")
    assert [s.find("MatchCode").text for s in services] == ["AWS", "DPW", "VPR"]
    assert all(s.find("WorkUnit").text == "3" for s in services)


def test_article_without_match_codes_emits_no_services_elements():
    article = _article(service_match_codes=[])
    root = _root(order=_order(order_items=[article]))
    item = root.find(f".//{DSI}Order/Items")
    assert item.findall("Services") == []


def test_decimal_amounts_keep_trailing_zeros_for_consistent_formatting():
    """0.200, 37.00 must serialise with their trailing zeros (DHL expects fixed-point)."""
    article = _article(volume_cbm=Decimal("0.200"), weight_kg=Decimal("37.00"))
    root = _root(order=_order(order_items=[article]))
    item = root.find(f".//{DSI}Order/Items")
    assert item.find("Volume/Amount").text == "0.200"
    assert item.find("Weight/Amount").text == "37.00"


def test_zero_volume_does_not_break_xml():
    article = _article(volume_cbm=Decimal(0))
    root = _root(order=_order(order_items=[article]))
    assert root.find(f".//{DSI}Order/Items/Volume/Amount").text == "0"


def test_packages_defaults_to_1_when_none():
    article = _article(packages=None)
    root = _root(order=_order(order_items=[article]))
    assert root.find(f".//{DSI}Order/Items/Packages").text == "1"


def test_default_work_unit_can_be_overridden():
    article = _article(service_match_codes=["AG"])
    builder = OrderXmlBuilder(work_unit=5)
    root = _root(builder, _order(order_items=[article]))
    assert root.find(f".//{DSI}Order/Items/Services/WorkUnit").text == "5"
