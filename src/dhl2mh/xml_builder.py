"""Build DHL DeliverIT (DSI / it4logistics) order XML.

Port of the C# OrderXmlGenerator. Only articles (stock_limitation 0 or 1) are
emitted as <Items> elements — service items have already been folded into their
article's service_match_codes by service_resolver, so they have nothing to
contribute at XML level.
"""

import time
from datetime import datetime
from decimal import Decimal
from typing import Final

from lxml import etree

from dhl2mh.mapping import STOCK_LIMITATION_ARTICLE
from dhl2mh.models import OrderItem, PlentyOrder

DSI_NS: Final = "http://www.it4logistics.de/i4ldata/ext"
XSI_NS: Final = "http://www.w3.org/2001/XMLSchema-instance"


class OrderXmlBuilder:
    """Stateless builder. Constructor args are the per-environment knobs:
    sender_partner_id is "1" for UAT, "3" for production (DHL convention)."""

    def __init__(
        self,
        *,
        sending_party_id: str = "HDE",
        receiving_party_id: str = "DELIVERIT",
        sender_partner_id: str = "1",
        message_structure_version: str = "2.23",
        order_type: str = "LIEF_KK",
        product_type: str = "ZH",
        freight_terms: str = "FV",
        work_unit: int = 3,
    ) -> None:
        self.sending_party_id = sending_party_id
        self.receiving_party_id = receiving_party_id
        self.sender_partner_id = sender_partner_id
        self.message_structure_version = message_structure_version
        self.order_type = order_type
        self.product_type = product_type
        self.freight_terms = freight_terms
        self.work_unit = work_unit

    def build(self, order: PlentyOrder) -> bytes:
        """Returns the order as a UTF-8 encoded XML document with declaration."""
        root = self._build_root(order)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    # ── construction ──────────────────────────────────────────────────────

    def _build_root(self, order: PlentyOrder) -> etree._Element:
        now = _now_with_tz()
        root = etree.Element(etree.QName(DSI_NS, "Transmission"), nsmap={"dsi": DSI_NS})
        _text(root, "TransmissionCreationDate", _format_dt(now))
        _text(root, "TransmissionControlNumber", _control_number())
        _text(root, "SendingPartyID", self.sending_party_id)
        _text(root, "ReceivingPartyID", self.receiving_party_id)
        _text(root, "MessageCount", "1")

        messages = etree.SubElement(root, "Messages")
        _text(messages, "MessageStructureVersion", self.message_structure_version)
        _text(messages, "MessageCreationDate", _format_dt(now))
        _text(messages, "MessageControlNumber", "1")

        msg_content = etree.SubElement(messages, "MessageContent")
        self._build_order(msg_content, order)
        return root

    def _build_order(self, parent: etree._Element, order: PlentyOrder) -> None:
        order_el = etree.SubElement(parent, etree.QName(DSI_NS, "Order"))

        order_id = etree.SubElement(order_el, "OrderId")
        _text(order_id, "System", self.sending_party_id)
        _text(order_id, "Id", str(order.id))

        _text(order_el, "OrderNr", str(order.id))

        self._build_sender(order_el)
        self._build_receiver(order_el, order)

        _text(order_el, "OrderType", self.order_type)
        _text(order_el, "ProductType", self.product_type)
        _text(order_el, "FreightTerms", self.freight_terms)
        _text(order_el, "OrderDate", order.order_date.strftime("%Y-%m-%d"))

        for item in order.order_items:
            if item.stock_limitation in STOCK_LIMITATION_ARTICLE:
                self._build_item(order_el, item)

    def _build_sender(self, parent: etree._Element) -> None:
        sender = etree.SubElement(parent, "Sender", nsmap={"xsi": XSI_NS})
        sender.set(etree.QName(XSI_NS, "type"), "dsi:Supplier")
        partner = etree.SubElement(sender, "PartnerId")
        _text(partner, "System", self.sending_party_id)
        _text(partner, "Id", self.sender_partner_id)

    def _build_receiver(self, parent: etree._Element, order: PlentyOrder) -> None:
        if not order.addresses:
            raise ValueError(f"Order {order.id} has no delivery address")
        addr = order.addresses[0]

        receiver = etree.SubElement(parent, "Receiver", nsmap={"xsi": XSI_NS})
        receiver.set(etree.QName(XSI_NS, "type"), "dsi:Customer")

        partner = etree.SubElement(receiver, "PartnerId")
        _text(partner, "System", self.sending_party_id)
        _text(partner, "Id", str(addr.customer_id))

        _text(receiver, "Name", addr.full_name)

        address = etree.SubElement(receiver, "Address")
        _text(address, "Name1", addr.full_name)
        _text(address, "Name2", "")
        _text(address, "CountryCode", addr.country_code or "DE")
        _text(address, "PostalCode", addr.postal_code or "")
        _text(address, "City", addr.city or "")
        _text(address, "Street", addr.street or "")
        _text(address, "PhoneNumber1", addr.phone_number or "")
        _text(address, "PhoneNumber2", "")
        _text(address, "EMail", addr.email or "")

    def _build_item(self, parent: etree._Element, item: OrderItem) -> None:
        items_el = etree.SubElement(parent, "Items")
        _text(items_el, "CatalogNr", str(item.id))
        _text(items_el, "ProductName", item.name or "")
        _text(items_el, "Quantity", _decimal_str(item.quantity))
        _text(items_el, "Packages", _decimal_str(item.packages or Decimal(1)))

        volume = etree.SubElement(items_el, "Volume")
        _text(volume, "Amount", _decimal_str(item.volume_cbm))
        _text(volume, "Unit", "CBM")

        weight = etree.SubElement(items_el, "Weight")
        _text(weight, "Amount", _decimal_str(item.weight_kg))
        _text(weight, "Unit", "KG")

        for match_code in item.service_match_codes:
            services = etree.SubElement(items_el, "Services")
            _text(services, "MatchCode", match_code)
            _text(services, "WorkUnit", str(self.work_unit))


# ── helpers ────────────────────────────────────────────────────────────────


def _text(parent: etree._Element, name: str, text: str) -> etree._Element:
    el = etree.SubElement(parent, name)
    el.text = text
    return el


def _now_with_tz() -> datetime:
    return datetime.now().astimezone()


def _format_dt(dt: datetime) -> str:
    """ISO 8601 with second precision and ±HH:MM offset (matches C# 'zzz')."""
    return dt.isoformat(timespec="seconds")


def _control_number() -> str:
    """10-digit time-based pseudo-unique number (matches the C# pattern)."""
    return str(time.time_ns())[-10:]


def _decimal_str(value: Decimal | int | None) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
