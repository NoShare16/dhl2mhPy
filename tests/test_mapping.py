from decimal import Decimal

import pytest

from dhl2mh.mapping import (
    HEAVY_LIFT_MATCH_CODE,
    HEAVY_LIFT_SERVICE_ID,
    HEAVY_LIFT_THRESHOLD_KG,
    HERDE_CATEGORY_IDS,
    SERVICE_AG,
    SERVICE_AWS,
    SERVICE_AWS_DPW,
    SERVICE_DI,
    SERVICE_DPW,
    SERVICE_EAN,
    SERVICE_INSTALL,
    SERVICE_ISEK,
    SERVICE_KF_EAN,
    SERVICE_LA,
    SERVICE_SVG,
    SERVICE_SWG,
    SERVICE_VPR,
    SERVICE_WHITELIST,
    VPR_MATCH_CODE,
    VPR_SERVICE_ID,
    VPR_TRIGGER_MATCH_CODES,
    UnknownServiceIdError,
    map_to_match_codes,
)


@pytest.mark.parametrize(
    "service_id, expected",
    [
        (SERVICE_AG, ["AG"]),
        (SERVICE_AWS, ["AWS"]),
        (SERVICE_DPW, ["DPW"]),
        (SERVICE_ISEK, ["ISEK"]),
        (SERVICE_EAN, ["E-AN"]),
        (SERVICE_SVG, ["SVG"]),
        (SERVICE_LA, ["LA"]),
        (SERVICE_DI, ["DI"]),
        (SERVICE_SWG, ["SWG"]),
        (SERVICE_VPR, ["VPR"]),
    ],
)
def test_single_code_services(service_id, expected):
    assert map_to_match_codes(service_id, []) == expected
    # categories must not affect static mappings
    assert map_to_match_codes(service_id, list(HERDE_CATEGORY_IDS)) == expected


def test_aws_dpw_service_emits_two_codes():
    assert map_to_match_codes(SERVICE_AWS_DPW, []) == ["AWS", "DPW"]


def test_kf_ean_service_emits_two_codes():
    assert map_to_match_codes(SERVICE_KF_EAN, []) == ["KF", "E-AN"]


def test_install_service_default_is_IS():
    assert map_to_match_codes(SERVICE_INSTALL, []) == ["IS"]
    assert map_to_match_codes(SERVICE_INSTALL, ["any-non-herde-cat"]) == ["IS"]


def test_install_service_flips_to_EAN_for_herde_categories():
    for herde_id in HERDE_CATEGORY_IDS:
        assert map_to_match_codes(SERVICE_INSTALL, [herde_id]) == ["E-AN"]


def test_install_service_herde_takes_precedence_when_mixed():
    cats = list(HERDE_CATEGORY_IDS) + ["other-cat"]
    assert map_to_match_codes(SERVICE_INSTALL, cats) == ["E-AN"]


def test_unknown_service_id_raises():
    with pytest.raises(UnknownServiceIdError, match="999999"):
        map_to_match_codes(999999, [])


def test_whitelist_contains_all_13_known_service_ids():
    expected = {
        SERVICE_AG, SERVICE_AWS_DPW, SERVICE_AWS, SERVICE_DPW,
        SERVICE_ISEK, SERVICE_KF_EAN, SERVICE_EAN, SERVICE_SVG,
        SERVICE_LA, SERVICE_DI, SERVICE_INSTALL, SERVICE_SWG, SERVICE_VPR,
    }
    assert SERVICE_WHITELIST == expected
    assert len(SERVICE_WHITELIST) == 13
    for sid in SERVICE_WHITELIST:
        map_to_match_codes(sid, [])  # no orphans


def test_heavy_lift_constants():
    assert HEAVY_LIFT_SERVICE_ID == SERVICE_SWG
    assert HEAVY_LIFT_THRESHOLD_KG == Decimal("120")
    assert HEAVY_LIFT_MATCH_CODE == "SWG"


def test_vpr_constants():
    assert VPR_SERVICE_ID == SERVICE_VPR
    assert VPR_MATCH_CODE == "VPR"
    assert VPR_TRIGGER_MATCH_CODES == frozenset({"AWS", "ISEK", "KF", "E-AN", "IS"})
