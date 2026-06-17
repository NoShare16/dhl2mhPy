from datetime import datetime
from decimal import Decimal

import pytest

from dhl2mh.mapping import (
    HERDE_CATEGORY_IDS,
    SERVICE_AG,
    SERVICE_AWS_DPW,
    SERVICE_INSTALL,
    SERVICE_KF_EAN,
    SERVICE_SWG,
    SERVICE_VPR,
)
from dhl2mh.models import OrderItem, PlentyOrder
from dhl2mh.service_resolver import resolve_order, resolve_orders


# ── builders ───────────────────────────────────────────────────────────────


def _order(items: list[OrderItem]) -> PlentyOrder:
    return PlentyOrder(
        id=1,
        status_id=6.1,
        type_id=1,
        order_date=datetime(2025, 1, 15),
        order_items=items,
    )


def _article(
    item_id: int = 100,
    *,
    bundle_id: str | None = None,
    former_parent_id: str | None = None,
    weight_g: int = 5000,
    width_mm: int = 1000,
    length_mm: int = 500,
    height_mm: int = 400,
    categories: list[str] | None = None,
) -> OrderItem:
    return OrderItem(
        id=item_id,
        bundle_id=bundle_id,
        former_parent_id=former_parent_id,
        stock_limitation=0,
        weight_g=Decimal(weight_g),
        width_mm=width_mm,
        length_mm=length_mm,
        height_mm=height_mm,
        categories=categories or [],
    )


def _service(
    item_id: int, *, bundle_id: str | None = None, former_parent_id: str | None = None
) -> OrderItem:
    return OrderItem(
        id=item_id,
        bundle_id=bundle_id,
        former_parent_id=former_parent_id,
        stock_limitation=2,
    )


# ── single-service mapping ─────────────────────────────────────────────────


def test_bundle_with_one_service_resolves_match_code():
    article = _article(bundle_id="X")
    service = _service(SERVICE_AG, bundle_id="X")
    resolve_order(_order([article, service]))

    assert article.service_ids == [SERVICE_AG]
    assert article.service_match_codes == ["AG"]


def test_service_folds_into_article_via_shopware_former_parent_override():
    """Plenty bundle_ids differ, but a shared Shopware former_parent_id groups
    the service with its article — this is the assignment DHL relies on."""
    article = _article(item_id=100, bundle_id="P-A", former_parent_id="SW-U")
    service = _service(SERVICE_AG, bundle_id="P-B", former_parent_id="SW-U")
    resolve_order(_order([article, service]))

    assert article.service_ids == [SERVICE_AG]
    assert article.service_match_codes == ["AG"]


def test_install_service_on_festwasser_article_resolves_to_AWS():
    """festwasser article → SERVICE_INSTALL emits AWS (which then auto-adds VPR)."""
    article = _article(bundle_id="X")
    article.festwasser = True
    service = _service(SERVICE_INSTALL, bundle_id="X")
    resolve_order(_order([article, service]))

    assert article.service_match_codes == ["AWS", "VPR"]


def test_service_emitting_two_codes_appends_both():
    article = _article(bundle_id="X")
    service = _service(SERVICE_AWS_DPW, bundle_id="X")
    resolve_order(_order([article, service]))

    # AWS triggers VPR auto-add, so both codes appear plus VPR
    assert article.service_match_codes == ["AWS", "DPW", "VPR"]


def test_install_service_uses_article_categories_for_IS_vs_EAN():
    # Both E-AN and IS trigger VPR auto-add, so VPR appears in both branches.
    herde_article = _article(bundle_id="H", categories=[next(iter(HERDE_CATEGORY_IDS))])
    install = _service(SERVICE_INSTALL, bundle_id="H")
    resolve_order(_order([herde_article, install]))
    assert herde_article.service_match_codes == ["E-AN", "VPR"]

    normal_article = _article(item_id=200, bundle_id="N", categories=["some-other-cat"])
    install2 = _service(SERVICE_INSTALL, bundle_id="N")
    resolve_order(_order([normal_article, install2]))
    assert normal_article.service_match_codes == ["IS", "VPR"]


# ── heavy-lift auto-add ────────────────────────────────────────────────────


def test_heavy_lift_auto_adds_SWG_when_article_over_120kg():
    article = _article(weight_g=150_000)  # 150 kg
    resolve_order(_order([article]))

    assert SERVICE_SWG in article.service_ids
    assert "SWG" in article.service_match_codes


def test_heavy_lift_not_added_under_threshold():
    article = _article(weight_g=120_000)  # exactly 120kg → NOT > 120
    resolve_order(_order([article]))

    assert SERVICE_SWG not in article.service_ids
    assert "SWG" not in article.service_match_codes


def test_heavy_lift_not_duplicated_when_plenty_already_has_SWG():
    article = _article(weight_g=200_000, bundle_id="X")
    swg = _service(SERVICE_SWG, bundle_id="X")
    resolve_order(_order([article, swg]))

    assert article.service_ids.count(SERVICE_SWG) == 1
    assert article.service_match_codes.count("SWG") == 1


# ── VPR auto-add ───────────────────────────────────────────────────────────


def test_vpr_auto_added_when_AWS_in_match_codes():
    article = _article(bundle_id="X")
    service = _service(SERVICE_AWS_DPW, bundle_id="X")  # → AWS, DPW
    resolve_order(_order([article, service]))

    assert "VPR" in article.service_match_codes
    assert SERVICE_VPR in article.service_ids


def test_vpr_auto_added_when_EAN_via_KF_EAN_service():
    article = _article(bundle_id="X")
    service = _service(SERVICE_KF_EAN, bundle_id="X")  # → KF, E-AN
    resolve_order(_order([article, service]))

    assert article.service_match_codes == ["KF", "E-AN", "VPR"]


def test_vpr_auto_added_when_IS_via_install_on_non_herde():
    article = _article(bundle_id="X", categories=["non-herde"])
    install = _service(SERVICE_INSTALL, bundle_id="X")
    resolve_order(_order([article, install]))

    assert "IS" in article.service_match_codes
    assert "VPR" in article.service_match_codes


def test_vpr_not_added_when_only_AG_present():
    """AG is not a VPR trigger."""
    article = _article(bundle_id="X")
    service = _service(SERVICE_AG, bundle_id="X")
    resolve_order(_order([article, service]))

    assert "VPR" not in article.service_match_codes


def test_vpr_not_added_for_swg_only():
    """SWG is not a VPR trigger."""
    article = _article(weight_g=200_000)
    resolve_order(_order([article]))

    assert article.service_match_codes == ["SWG"]
    assert "VPR" not in article.service_match_codes


def test_vpr_not_duplicated_when_plenty_already_has_VPR():
    article = _article(bundle_id="X")
    aws = _service(SERVICE_AWS_DPW, bundle_id="X")  # would trigger VPR
    vpr = _service(SERVICE_VPR, bundle_id="X")  # already present
    resolve_order(_order([article, aws, vpr]))

    assert article.service_match_codes.count("VPR") == 1
    assert article.service_ids.count(SERVICE_VPR) == 1


# ── standalone article ────────────────────────────────────────────────────


def test_standalone_article_with_no_services():
    article = _article()
    resolve_order(_order([article]))

    assert article.service_ids == []
    assert article.service_match_codes == []


def test_multiple_bundles_resolved_independently():
    a1 = _article(item_id=1, bundle_id="X")
    s1 = _service(SERVICE_AG, bundle_id="X")
    a2 = _article(item_id=2, bundle_id="Y")
    s2 = _service(SERVICE_KF_EAN, bundle_id="Y")

    resolve_order(_order([a1, s1, a2, s2]))

    assert a1.service_match_codes == ["AG"]
    assert a2.service_match_codes == ["KF", "E-AN", "VPR"]


# ── derived fields ─────────────────────────────────────────────────────────


def test_weight_kg_derived_from_weight_g():
    article = _article(weight_g=37_500)
    resolve_order(_order([article]))
    assert article.weight_kg == Decimal("37.50")


def test_weight_kg_is_none_when_weight_g_zero():
    article = _article(weight_g=0)
    resolve_order(_order([article]))
    assert article.weight_kg is None


def test_volume_cbm_derived_from_mm_dimensions():
    # 1000mm * 500mm * 400mm = 200_000_000 mm³ = 0.200 m³
    article = _article(width_mm=1000, length_mm=500, height_mm=400)
    resolve_order(_order([article]))
    assert article.volume_cbm == Decimal("0.200")


def test_volume_cbm_zero_when_a_dimension_missing():
    article = _article(width_mm=1000, length_mm=0, height_mm=400)
    resolve_order(_order([article]))
    assert article.volume_cbm == Decimal(0)


# ── non-service StockLimitation-2 positions (discounts) are ignored ────────


def test_resolve_orders_ignores_non_whitelisted_stock2_positions():
    """A discount (stock 2, non-whitelisted id) is not a service: order passes."""
    order = _order(
        [_article(item_id=2, bundle_id="X"), _service(999999, bundle_id="X")]
    )

    result = resolve_orders([order])

    assert result.passed == [order]
    assert result.skipped == []


def test_resolve_order_treats_non_whitelisted_stock2_as_no_service():
    article = _article(bundle_id="X")
    discount = _service(999999, bundle_id="X")  # stock 2 but not a real service
    resolve_order(_order([article, discount]))

    # The discount contributes nothing — article has no service match codes.
    assert article.service_ids == []
    assert article.service_match_codes == []
