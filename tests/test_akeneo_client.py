import json as jsonlib

import httpx
import pytest
import respx

from dhl2mh.clients.akeneo import AkeneoAuthError, AkeneoClient, AkeneoProductLookup

MK_URL = "https://pim-mk.test"
ML_URL = "https://pim-ml.test"


def _product(modell: str | None = "UG 5005-30", color: str | None = "kupfer_rose") -> dict:
    """One /products hit in the Akeneo _embedded shape."""
    values: dict[str, list[dict]] = {}
    if modell is not None:
        values["modell"] = [{"locale": None, "scope": None, "data": modell}]
    if color is not None:
        values["color"] = [{"locale": None, "scope": None, "data": color}]
    return {"_embedded": {"items": [{"identifier": "4002511099043", "values": values}]}}


EMPTY = {"_embedded": {"items": []}}


def _token_route(router) -> object:
    return router.post("/api/oauth/v1/token").respond(
        200, json={"access_token": "tok", "expires_in": 3600}
    )


async def test_get_product_info_resolves_model_and_color_label(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        products = router.get("/api/rest/v1/products").respond(200, json=_product())
        router.get("/api/rest/v1/attributes/color/options/kupfer_rose").respond(
            200, json={"code": "kupfer_rose", "labels": {"de_DE": "Kupfer Rose"}}
        )

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            info = await c.get_product_info(773596)

        assert info is not None
        assert info.model == "UG 5005-30"
        # The product API returns the option code; the label is what we want.
        assert info.color == "Kupfer Rose"

        # Lookup goes through the unique plenty_varianten_id attribute.
        search = jsonlib.loads(products.calls[0].request.url.params["search"])
        assert search == {"plenty_varianten_id": [{"operator": "=", "value": "773596"}]}
        assert products.calls[0].request.url.params["attributes"] == "modell,color"


async def test_unknown_variation_returns_none(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=EMPTY)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            assert await c.get_product_info(999999) is None


async def test_unresolvable_color_option_falls_back_to_the_code(akeneo_settings):
    """A 404 on the option must not lose the color — the code still reads."""
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=_product(color="mystery"))
        router.get("/api/rest/v1/attributes/color/options/mystery").respond(404)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            info = await c.get_product_info(1)

        assert info is not None
        assert info.color == "mystery"


async def test_color_label_is_cached_per_code(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=_product(color="schwarz"))
        option = router.get("/api/rest/v1/attributes/color/options/schwarz").respond(
            200, json={"labels": {"de_DE": "Schwarz"}}
        )

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            await c.get_product_info(1)
            await c.get_product_info(2)
            await c.get_product_info(3)

        assert option.call_count == 1


async def test_product_without_color_yields_model_only(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=_product(color=None))

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            info = await c.get_product_info(1)

        assert info is not None
        assert info.model == "UG 5005-30"
        assert info.color is None


@pytest.mark.parametrize("code", ["empty", "Nicht_zutreffend"])
async def test_placeholder_color_is_dropped_not_appended(akeneo_settings, code):
    """"keine Angabe" must never reach the DHL ProductName."""
    # assert_all_called=False: the option route deliberately stays untouched.
    with respx.mock(base_url=MK_URL, assert_all_called=False) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=_product(color=code))
        option = router.get(f"/api/rest/v1/attributes/color/options/{code}").respond(
            200, json={"labels": {"de_DE": "keine Angabe"}}
        )

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            info = await c.get_product_info(12522)

        assert info is not None
        assert info.model == "UG 5005-30"
        assert info.color is None
        # Recognised by code, so the label lookup is not even attempted.
        assert option.call_count == 0


async def test_real_color_resembling_a_placeholder_is_kept(akeneo_settings):
    """Suppression is by code — a genuine color keeps its label."""
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(
            200, json=_product(color="braun_anthrazit_meliert")
        )
        router.get("/api/rest/v1/attributes/color/options/braun_anthrazit_meliert").respond(
            200, json={"labels": {"de_DE": "Braun/Anthrazit Meliert"}}
        )

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            info = await c.get_product_info(1)

        assert info is not None
        assert info.color == "Braun/Anthrazit Meliert"


async def test_search_failure_propagates(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/products").respond(500, text="boom")

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await c.get_product_info(1)


async def test_login_failure_raises(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        router.post("/api/oauth/v1/token").respond(401, text="nope")

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            with pytest.raises(AkeneoAuthError, match="HTTP 401"):
                await c.get_product_info(1)


async def test_login_uses_password_grant_with_basic_client_auth(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        token = _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=EMPTY)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            await c.get_product_info(1)

        body = jsonlib.loads(token.calls[0].request.content)
        assert body["grant_type"] == "password"
        assert body["username"] == "pim-user"
        assert body["password"] == "pim-pw"
        # client_id/secret travel as HTTP Basic, not in the body.
        assert token.calls[0].request.headers["Authorization"].startswith("Basic ")


async def test_token_cached_across_requests(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        token = _token_route(router)
        router.get("/api/rest/v1/products").respond(200, json=EMPTY)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            await c.get_product_info(1)
            await c.get_product_info(2)

        assert token.call_count == 1


async def test_401_triggers_token_refresh(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        token = router.post("/api/oauth/v1/token")
        token.mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
                httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
            ]
        )
        products = router.get("/api/rest/v1/products")
        products.mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json=EMPTY)]
        )

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            await c.get_product_info(1)

        assert token.call_count == 2
        assert products.calls[-1].request.headers["Authorization"] == "Bearer tok-2"


async def test_expired_token_triggers_refresh_without_401(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        token = router.post("/api/oauth/v1/token")
        token.mock(
            side_effect=[
                # First token expires inside the refresh buffer → already stale.
                httpx.Response(200, json={"access_token": "tok-1", "expires_in": 1}),
                httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
            ]
        )
        router.get("/api/rest/v1/products").respond(200, json=EMPTY)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            await c.get_product_info(1)
            await c.get_product_info(2)

        assert token.call_count == 2


async def test_bulk_keyed_by_variation_id_omits_misses(akeneo_settings):
    with respx.mock(base_url=MK_URL) as router:
        _token_route(router)
        router.get("/api/rest/v1/attributes/color/options/schwarz").respond(
            200, json={"labels": {"de_DE": "Schwarz"}}
        )

        known = {"100": "E 216", "200": "E 313"}

        def handler(request: httpx.Request) -> httpx.Response:
            search = jsonlib.loads(request.url.params["search"])
            vid = search["plenty_varianten_id"][0]["value"]
            if vid not in known:
                return httpx.Response(200, json=EMPTY)
            return httpx.Response(200, json=_product(modell=known[vid], color="schwarz"))

        router.get("/api/rest/v1/products").mock(side_effect=handler)

        async with AkeneoClient(
            "MK", akeneo_settings.akeneomk, akeneo_settings.akeneo
        ) as c:
            result = await c.get_product_infos_bulk([100, 200, 300], concurrency=2)

        assert set(result) == {"100", "200"}
        assert result["100"].model == "E 216"
        assert result["200"].color == "Schwarz"


# ── AkeneoProductLookup: MK first, ML for the leftovers ─────────────────────


async def test_lookup_falls_through_to_ml_for_unmatched_articles(akeneo_settings):
    with respx.mock() as router:
        router.post(f"{MK_URL}/api/oauth/v1/token").respond(
            200, json={"access_token": "mk", "expires_in": 3600}
        )
        router.post(f"{ML_URL}/api/oauth/v1/token").respond(
            200, json={"access_token": "ml", "expires_in": 3600}
        )

        def responder(known: dict[str, str]):
            def handler(request: httpx.Request) -> httpx.Response:
                search = jsonlib.loads(request.url.params["search"])
                vid = search["plenty_varianten_id"][0]["value"]
                if vid not in known:
                    return httpx.Response(200, json=EMPTY)
                return httpx.Response(200, json=_product(modell=known[vid], color=None))

            return handler

        mk = router.get(f"{MK_URL}/api/rest/v1/products")
        mk.mock(side_effect=responder({"100": "E 216"}))
        ml = router.get(f"{ML_URL}/api/rest/v1/products")
        ml.mock(side_effect=responder({"200": "Crown Pellet 500"}))

        async with AkeneoProductLookup(akeneo_settings) as pim:
            result = await pim.get_product_infos_bulk([100, 200, 300], concurrency=3)

        assert result["100"].model == "E 216"
        assert result["200"].model == "Crown Pellet 500"
        assert "300" not in result

        # MK saw all three; ML only the two MK could not answer.
        assert mk.call_count == 3
        assert ml.call_count == 2


async def test_lookup_treats_a_hit_without_modell_as_unmatched(akeneo_settings):
    """A PIM entry with no ``modell`` yields no name, so ML still gets a turn."""
    with respx.mock() as router:
        router.post(f"{MK_URL}/api/oauth/v1/token").respond(
            200, json={"access_token": "mk", "expires_in": 3600}
        )
        router.post(f"{ML_URL}/api/oauth/v1/token").respond(
            200, json={"access_token": "ml", "expires_in": 3600}
        )
        router.get(f"{MK_URL}/api/rest/v1/products").respond(
            200, json=_product(modell=None, color=None)
        )
        ml = router.get(f"{ML_URL}/api/rest/v1/products").respond(
            200, json=_product(modell="Regal S590", color=None)
        )

        async with AkeneoProductLookup(akeneo_settings) as pim:
            result = await pim.get_product_infos_bulk([100])

        assert ml.call_count == 1
        assert result["100"].model == "Regal S590"


async def test_lookup_disabled_without_credentials(settings):
    """The stock settings fixture has no Akeneo config → nothing to query."""
    async with AkeneoProductLookup(settings) as pim:
        assert pim.enabled is False
        assert await pim.get_product_infos_bulk([1, 2]) == {}
