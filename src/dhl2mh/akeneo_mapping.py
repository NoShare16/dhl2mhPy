"""Build the DHL ProductName from Akeneo PIM data.

The **primary** source, ahead of Shopware: Shopware's ``manufacturerNumber``
only carries the numeric article id ("1514720"), while the PIM's ``modell``
attribute holds the readable model designation ("UG 5005-30"). The two differ
for the large majority of articles, which is the point of the switch.
"""

from dhl2mh.models import AkeneoProductInfo


def akeneo_model_name(info: AkeneoProductInfo) -> str | None:
    """DHL ProductName from Akeneo: ``modell`` + color label.

    ``None`` without a ``modell`` — the caller then tries Shopware and, failing
    that, skips the order. A missing *color* only drops that part: the model on
    its own is a perfectly good name, so it is not a reason to fall through.
    """
    model = (info.model or "").strip()
    if not model:
        return None
    color = (info.color or "").strip()
    return f"{model} {color}" if color else model
