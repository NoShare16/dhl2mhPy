from dhl2mh.akeneo_mapping import akeneo_display_name
from dhl2mh.models import AkeneoProduct, AkeneoProductInfo


def test_model_and_color_are_combined():
    info = AkeneoProductInfo(model="UG 5005-30", color="Schwarz")
    assert akeneo_display_name(info, fallback="1514720 Schwarz") == "UG 5005-30 Schwarz"


def test_missing_color_keeps_the_model_alone():
    """The model still beats the numeric Shopware fallback."""
    info = AkeneoProductInfo(model="UG 5005-30", color=None)
    assert akeneo_display_name(info, fallback="1514720 Schwarz") == "UG 5005-30"


def test_missing_model_falls_back():
    info = AkeneoProductInfo(model=None, color="Schwarz")
    assert akeneo_display_name(info, fallback="1514720 Schwarz") == "1514720 Schwarz"


def test_blank_model_falls_back():
    info = AkeneoProductInfo(model="   ", color="Schwarz")
    assert akeneo_display_name(info, fallback="Plenty-Name") == "Plenty-Name"


def test_fallback_may_be_absent():
    assert akeneo_display_name(AkeneoProductInfo(), fallback=None) is None


def test_values_are_trimmed():
    info = AkeneoProductInfo(model="  E 216  ", color="  Weiss  ")
    assert akeneo_display_name(info, fallback=None) == "E 216 Weiss"


# ── AkeneoProduct.scalar: the {locale, scope, data} value shape ─────────────


def test_scalar_reads_the_single_unlocalised_value():
    p = AkeneoProduct.model_validate(
        {"identifier": "x", "values": {"modell": [{"locale": None, "scope": None, "data": "E 9"}]}}
    )
    assert p.scalar("modell") == "E 9"


def test_scalar_returns_none_for_absent_or_empty_attributes():
    p = AkeneoProduct.model_validate(
        {
            "values": {
                "modell": [{"locale": None, "scope": None, "data": None}],
                "color": [{"locale": None, "scope": None, "data": "   "}],
            }
        }
    )
    assert p.scalar("modell") is None
    assert p.scalar("color") is None
    assert p.scalar("does_not_exist") is None


def test_scalar_stringifies_numbers():
    """plenty_varianten_id is a number attribute; a numeric modell is possible too."""
    p = AkeneoProduct.model_validate(
        {"values": {"modell": [{"locale": None, "scope": None, "data": 60684}]}}
    )
    assert p.scalar("modell") == "60684"


def test_scalar_ignores_composite_values():
    p = AkeneoProduct.model_validate(
        {"values": {"modell": [{"locale": None, "scope": None, "data": {"amount": 1}}]}}
    )
    assert p.scalar("modell") is None
