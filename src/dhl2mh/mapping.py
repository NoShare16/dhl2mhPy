"""DHL DeliverIT service mappings and filter-stage business constants.

A Plenty service item (StockLimitation==2) maps to one *or more* DHL MatchCodes.
Two mappings depend on context:

* SERVICE_INSTALL (783139) → "AWS" when the article needs a fixed water
  connection (Festwasser, takes precedence), else "E-AN" when the article is a
  "Herd" (Shopware category), else "IS".
* SERVICE_SWG (heavy-lift) and SERVICE_VPR are auto-attached by the filter
  based on weight / other MatchCodes already on the article.
"""

from decimal import Decimal
from typing import Final

# ── Plenty service variation IDs (item.StockLimitation == 2) ───────────────
SERVICE_AG: Final = 783116          # → AG
SERVICE_AWS_DPW: Final = 783117     # → AWS + DPW
SERVICE_AWS: Final = 783143         # → AWS
SERVICE_DPW: Final = 783148         # → DPW
SERVICE_ISEK: Final = 783149        # → ISEK
SERVICE_ISEK_KG: Final = 783172     # → ISEK ("Installationsservice - KG")
SERVICE_KF_EAN: Final = 783141      # → KF + E-AN
SERVICE_EAN: Final = 783140         # → E-AN
SERVICE_SVG: Final = 783146         # → SVG
SERVICE_LA: Final = 783145          # → LA
SERVICE_DI: Final = 783151          # → DI
SERVICE_INSTALL: Final = 783139     # → AWS (Festwasser) / E-AN (Herde) / IS (default)
SERVICE_SWG: Final = 783152         # → SWG (auto-attached when weight > 120 kg)
SERVICE_VPR: Final = 783138         # → VPR (auto-attached when triggering codes present)

SERVICE_WHITELIST: Final[frozenset[int]] = frozenset(
    {
        SERVICE_AG,
        SERVICE_AWS_DPW,
        SERVICE_AWS,
        SERVICE_DPW,
        SERVICE_ISEK,
        SERVICE_ISEK_KG,
        SERVICE_KF_EAN,
        SERVICE_EAN,
        SERVICE_SVG,
        SERVICE_LA,
        SERVICE_DI,
        SERVICE_INSTALL,
        SERVICE_SWG,
        SERVICE_VPR,
    }
)

# ── Plenty variation id ↔ Shopware productNumber ───────────────────────────
# A Plenty position normally matches its Shopware line item 1:1 by
# ``productNumber == str(variation id)``. The "Installationsservice - KG" breaks
# that: Shopware sends productNumber 783149, Plenty books variation 783172. The
# alias is only consulted when no line item carries the variation id itself.
SHOPWARE_PRODUCT_NUMBER_ALIASES: Final[dict[int, str]] = {
    SERVICE_ISEK_KG: str(SERVICE_ISEK),
}

# ── Auto-attach rules ──────────────────────────────────────────────────────
HEAVY_LIFT_SERVICE_ID: Final = SERVICE_SWG
HEAVY_LIFT_THRESHOLD_KG: Final = Decimal("120")
HEAVY_LIFT_MATCH_CODE: Final = "SWG"

VPR_SERVICE_ID: Final = SERVICE_VPR
VPR_MATCH_CODE: Final = "VPR"
# Presence of any of these on an article triggers an auto-attached VPR
VPR_TRIGGER_MATCH_CODES: Final[frozenset[str]] = frozenset(
    {"AWS", "ISEK", "KF", "E-AN", "IS"}
)

# ── Shopware category IDs for "Herde" — flips SERVICE_INSTALL from IS to E-AN
HERDE_CATEGORY_IDS: Final[frozenset[str]] = frozenset(
    {
        "01920f7e354f723aa41a2102249beb7f",
        "01920f7eba637642a6c29a5905bf961b",
    }
)

# ── Shopware property group "Wasseranschluss" — option value "ja" means the
# article needs a fixed water connection (Festwasser) → SERVICE_INSTALL emits AWS.
WATER_CONNECTION_GROUP_ID: Final = "8910dbddf00a4d94998289840033982d"
WATER_CONNECTION_MATCH_CODE: Final = "AWS"

# ── Shopware property group "Farbe" — the color option name is combined with the
# product's manufacturerNumber to form the DHL ProductName (see
# shopware_mapping.product_display_name).
COLOR_GROUP_ID: Final = "b7c2c23b73454356bec99f10042600eb"

# Plenty StockLimitation classification
STOCK_LIMITATION_ARTICLE: Final = (0, 1)
STOCK_LIMITATION_SERVICE: Final = 2

# Plenty order types that should be shipped (order-level ``type_id``). 1 =
# Verkaufsauftrag, plus 2 and 5 which also represent real, shippable orders.
# Everything else (returns, credit notes, …) is skipped by the filter.
SHIPPABLE_ORDER_TYPE_IDS: Final[frozenset[int]] = frozenset({1, 2, 5})


class UnknownServiceIdError(ValueError):
    pass


_STATIC_MATCH_CODES: Final[dict[int, tuple[str, ...]]] = {
    SERVICE_AG: ("AG",),
    SERVICE_AWS_DPW: ("AWS", "DPW"),
    SERVICE_AWS: ("AWS",),
    SERVICE_DPW: ("DPW",),
    SERVICE_ISEK: ("ISEK",),
    SERVICE_ISEK_KG: ("ISEK",),
    SERVICE_KF_EAN: ("KF", "E-AN"),
    SERVICE_EAN: ("E-AN",),
    SERVICE_SVG: ("SVG",),
    SERVICE_LA: ("LA",),
    SERVICE_DI: ("DI",),
    SERVICE_SWG: ("SWG",),
    SERVICE_VPR: ("VPR",),
}


def map_to_match_codes(
    service_id: int,
    category_ids: list[str] | frozenset[str],
    *,
    festwasser: bool = False,
) -> list[str]:
    """One service ID → one or more DHL MatchCodes (preserves order).

    Two IDs (783117 AWS+DPW, 783141 KF+E-AN) emit two codes from a single Plenty
    service item — they become two <Services> blocks in the DHL XML.

    SERVICE_INSTALL (783139) is context-dependent: "AWS" when the article needs a
    fixed water connection (Festwasser, takes precedence), else "E-AN" for Herde,
    else "IS".
    """
    if service_id == SERVICE_INSTALL:
        if festwasser:
            return [WATER_CONNECTION_MATCH_CODE]
        if any(cid in HERDE_CATEGORY_IDS for cid in category_ids):
            return ["E-AN"]
        return ["IS"]

    try:
        return list(_STATIC_MATCH_CODES[service_id])
    except KeyError:
        raise UnknownServiceIdError(f"Unbekannte Service-ID: {service_id}") from None
