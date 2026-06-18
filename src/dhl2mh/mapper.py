"""Map raw Plenty API DTOs (ApiOrder) to internal domain models (PlentyOrder).

Port of the C# PlentyOrderMapper, plus extraction of the bundle/group id from
item properties (typeId=1021) so the filter can group articles and services.
"""

from decimal import Decimal

from dhl2mh.models import (
    Address,
    ApiAddress,
    ApiOrder,
    ApiOrderItem,
    OrderItem,
    PlentyOrder,
)

# Plenty type-id constants (Plenty uses untyped integers throughout the API)
ADDRESS_RELATION_DELIVERY = 2
RECEIVER_RELATION = "receiver"
ADDRESS_OPTION_PHONE = 4
ADDRESS_OPTION_EMAIL = 5
ITEM_PROPERTY_BUNDLE_ID = 1021
ORDER_PROPERTY_SHOPWARE_ID = 7

# Plenty order-item types. A "Set" service (e.g. 783117 → AWS+DPW) comes back as
# a bundle PARENT (typeId 2) plus its fulfilment COMPONENTS (typeId 3, e.g.
# 783143/783147/783148). We read the parent — it carries the service id we map —
# and drop the components, which would otherwise emit duplicate/extra MatchCodes.
# The same service ordered standalone arrives as a normal position (typeId 1), so
# keying on the order-item typeId keeps that case working.
ORDER_ITEM_TYPE_ARTICLE = 1
ORDER_ITEM_TYPE_BUNDLE = 2
ORDER_ITEM_TYPE_BUNDLE_COMPONENT = 3
KEPT_ORDER_ITEM_TYPES = frozenset({ORDER_ITEM_TYPE_ARTICLE, ORDER_ITEM_TYPE_BUNDLE})
COUNTRY_FALLBACK = "FEHLER"


def map_order(api: ApiOrder, country_codes: dict[int, str]) -> PlentyOrder:
    """Convert one ApiOrder (raw Plenty REST shape) into the domain PlentyOrder."""
    return PlentyOrder(
        id=api.id,
        status_id=api.status_id,
        type_id=api.type_id,
        order_date=api.created_at,
        addresses=_map_addresses(api, country_codes),
        order_items=_map_order_items(api),
        package_number=_first_package_number(api),
        shopware_id=_get_order_property(api, ORDER_PROPERTY_SHOPWARE_ID),
    )


# ── helpers ────────────────────────────────────────────────────────────────


def _map_addresses(api: ApiOrder, country_codes: dict[int, str]) -> list[Address]:
    delivery_rel = next(
        (r for r in api.address_relations if r.type_id == ADDRESS_RELATION_DELIVERY),
        None,
    )
    if delivery_rel is None:
        return []

    address = next((a for a in api.addresses if a.id == delivery_rel.address_id), None)
    if address is None:
        return []

    customer_id = next(
        (r.reference_id for r in api.relations if r.relation == RECEIVER_RELATION),
        0,
    )

    return [
        Address(
            id=address.id,
            customer_id=customer_id,
            first_name=address.name2,
            last_name=address.name3,
            street=_join_street(address),
            postal_code=address.postal_code,
            city=address.town,
            country_code=country_codes.get(address.country_id, COUNTRY_FALLBACK),
            phone_number=_address_option(address, ADDRESS_OPTION_PHONE),
            email=_address_option(address, ADDRESS_OPTION_EMAIL),
        )
    ]


def _map_order_items(api: ApiOrder) -> list[OrderItem]:
    items: list[OrderItem] = []
    for it in api.order_items:
        if it.type_id not in KEPT_ORDER_ITEM_TYPES:
            continue
        variation = it.variation
        items.append(
            OrderItem(
                id=it.item_variation_id,
                name=it.order_item_name,
                quantity=it.quantity,
                stock_limitation=variation.stock_limitation if variation else 0,
                packages=it.quantity,
                # former_parent_id seeds from bundle_id automatically (OrderItem
                # validator); Shopware overwrites it later when a value exists.
                bundle_id=_get_item_property(it, ITEM_PROPERTY_BUNDLE_ID),
                weight_g=Decimal(variation.weight_g) if variation else None,
                height_mm=variation.height_mm if variation else 0,
                length_mm=variation.length_mm if variation else 0,
                width_mm=variation.width_mm if variation else 0,
            )
        )
    return items


def _join_street(address: ApiAddress) -> str:
    return f"{address.address1 or ''} {address.address2 or ''}".strip()


def _address_option(address: ApiAddress, type_id: int) -> str | None:
    return next(
        (opt.value for opt in address.options if opt.type_id == type_id),
        None,
    )


def _first_package_number(api: ApiOrder) -> str | None:
    """First non-empty package number across all shipping packages.

    Plenty keeps the original package at index 0 with an empty packageNumber and
    stores the assigned tracking number on a *later* package entry — so index 0
    alone misses already-shipped orders and the filter would re-process them.
    """
    for pkg in api.shipping_packages:
        if pkg.package_number:
            return pkg.package_number
    return None


def _get_order_property(api: ApiOrder, type_id: int) -> str | None:
    return next((p.value for p in api.properties if p.type_id == type_id), None)


def _get_item_property(item: ApiOrderItem, type_id: int) -> str | None:
    return next((p.value for p in item.properties if p.type_id == type_id), None)
