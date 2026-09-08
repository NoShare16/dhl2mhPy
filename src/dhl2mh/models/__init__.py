"""Data models, grouped by where they come from.

* ``base``     – shared pydantic config for raw API payloads
* ``plenty``   – Plenty REST DTOs (``Api*``)
* ``shopware`` – Shopware Admin-API DTOs (``Sw*``)
* ``akeneo``   – Akeneo PIM DTOs (``Akeneo*``)
* ``order``    – domain models the pipeline works on

Everything is re-exported here, so ``from dhl2mh.models import PlentyOrder``
keeps working; import from the submodule when the origin matters.
"""

from dhl2mh.models.akeneo import AkeneoProduct, AkeneoProductInfo, AkeneoValue
from dhl2mh.models.base import ApiModel
from dhl2mh.models.order import (
    AckError,
    Address,
    LabelInfo,
    OrderItem,
    PackageData,
    PlentyOrder,
    SkippedOrder,
)
from dhl2mh.models.plenty import (
    ApiAddress,
    ApiAddressOption,
    ApiAddressRelation,
    ApiCountry,
    ApiOrder,
    ApiOrderItem,
    ApiOrderPage,
    ApiProperty,
    ApiRelation,
    ApiShippingPackage,
    ApiVariation,
)
from dhl2mh.models.shopware import (
    SwLineItemPayload,
    SwOrder,
    SwOrderLineItem,
    SwProduct,
    SwProductInfo,
    SwPropertyOption,
)

__all__ = [
    "AckError",
    "Address",
    "AkeneoProduct",
    "AkeneoProductInfo",
    "AkeneoValue",
    "ApiAddress",
    "ApiAddressOption",
    "ApiAddressRelation",
    "ApiCountry",
    "ApiModel",
    "ApiOrder",
    "ApiOrderItem",
    "ApiOrderPage",
    "ApiProperty",
    "ApiRelation",
    "ApiShippingPackage",
    "ApiVariation",
    "LabelInfo",
    "OrderItem",
    "PackageData",
    "PlentyOrder",
    "SkippedOrder",
    "SwLineItemPayload",
    "SwOrder",
    "SwOrderLineItem",
    "SwProduct",
    "SwProductInfo",
    "SwPropertyOption",
]
