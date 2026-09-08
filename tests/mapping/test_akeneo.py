from dhl2mh.mapping.akeneo import akeneo_model_name
from dhl2mh.models import AkeneoProduct, AkeneoProductInfo


def test_model_and_color_are_combined():
    info = AkeneoProductInfo(model="UG 5005-30", color="Schwarz")
    assert akeneo_model_name(info) == "UG 5005-30 Schwarz"


def test_missing_color_keeps_the_model_alone():
    """A model without a color is still a perfectly good name."""
    info = AkeneoProductInfo(model="UG 5005-30", color=None)
    assert akeneo_model_name(info) == "UG 5005-30"


def test_missing_model_yields_nothing():
    """The caller then tries Shopware and, failing that, skips the order."""
    info = AkeneoProductInfo(model=None, color="Schwarz")
    assert akeneo_model_name(info) is None


def test_blank_model_yields_nothing():
    info = AkeneoProductInfo(model="   ", color="Schwarz")
    assert akeneo_model_name(info) is None


def test_empty_info_yields_nothing():
    assert akeneo_model_name(AkeneoProductInfo()) is None


def test_values_are_trimmed():
    info = AkeneoProductInfo(model="  E 216  ", color="  Weiss  ")
    assert akeneo_model_name(info) == "E 216 Weiss"


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
