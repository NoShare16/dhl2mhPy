"""Build the DHL ProductName from Akeneo PIM data.

Supersedes the Shopware-derived name: Shopware's ``manufacturerNumber`` only
carries the numeric article id ("1514720"), while the PIM's ``modell``
attribute holds the readable model designation ("UG 5005-30"). The two differ
for the large majority of articles, which is the whole point of the switch.
"""

from dhl2mh.models import AkeneoProductInfo


def akeneo_display_name(info: AkeneoProductInfo, *, fallback: str | None) -> str | None:
    """DHL ProductName from Akeneo: ``modell`` + color label.

    Without a ``modell`` there is nothing better than the ``fallback`` (the
    Shopware-derived name, itself falling back to the Plenty order_item_name).
    A missing color only drops that part — the model on its own is still the
    better name, so it is not a reason to fall back.
    """
    model = (info.model or "").strip()
    if not model:
        return fallback
    color = (info.color or "").strip()
    return f"{model} {color}" if color else model
