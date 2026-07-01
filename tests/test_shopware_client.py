import json as jsonlib
from pathlib import Path

import httpx
import pytest
import respx

from dhl2mh.clients.shopware import ShopwareAuthError, ShopwareClient
from dhl2mh.mapping import COLOR_GROUP_ID

SW_ORDER_FIXTURE = Path(__file__).parent / "fixtures" / "sw_order_mit_accept.json"
SW_PRODUCT_FIXTURE = Path(__file__).parent / "fixtures" / "sw_order_prop.json"


async def test_get_product_info_parses_categories_name_and_color(settings):
    product_json = jsonlib.loads(SW_PRODUCT_FIXTURE.read_text())
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        post = router.post("/api/search/product").respond(200, json=product_json)

        async with ShopwareClient(settings) as c:
            info = await c.get_product_info("768840")

        assert info is not None
        assert info.category_ids == ["01920f7e354f723aa41a2102249beb7f"]
        assert info.manufacturer_number == "HE517ABW0"
        # Color is the property option in the Farbe group. Derive the expected
        # value from the fixture to avoid a non-ASCII literal in the source.
        expected_color = next(
            p["name"]
            for p in product_json["data"][0]["properties"]
            if p["groupId"] == COLOR_GROUP_ID
        )
        assert expected_color  # sanity: the fixture actually carries a color
        assert info.color(COLOR_GROUP_ID) == expected_color

        # Request body now also asks for the properties association.
        body = jsonlib.loads(post.calls[0].request.content)
        assert body["filter"][0]["field"] == "productNumber"
        assert body["filter"][0]["value"] == "768840"
        assert body["associations"] == {"categories": {}, "properties": {}}


async def test_get_product_info_returns_none_when_no_data(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            info = await c.get_product_info(99999)

        assert info is None


async def test_search_failure_propagates(settings):
    """C# original swallowed errors and returned []. Python surfaces them."""
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(500, text="boom")

        async with ShopwareClient(settings) as c:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await c.get_product_info("42")


async def test_token_cached_across_requests(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        token_ep = router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            await c.get_product_info("1")
            await c.get_product_info("2")
            await c.get_product_info("3")

        assert token_ep.call_count == 1


async def test_401_triggers_token_refresh(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        token_ep = router.post("/api/oauth/token")
        token_ep.mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "tok-1", "expires_in": 600}),
                httpx.Response(200, json={"access_token": "tok-2", "expires_in": 600}),
            ]
        )
        search = router.post("/api/search/product")
        search.mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"data": []}),
            ]
        )

        async with ShopwareClient(settings) as c:
            await c.get_product_info("1")

        assert token_ep.call_count == 2
        assert search.calls[-1].request.headers["Authorization"] == "Bearer tok-2"
        assert search.calls[-1].request.headers["sw-access-key"] == "tok-2"


async def test_expired_token_triggers_refresh_without_401(settings, monkeypatch):
    """If expires_in is short, next call should refresh proactively."""
    with respx.mock(base_url=settings.shopware.base_url) as router:
        token_ep = router.post("/api/oauth/token")
        token_ep.mock(
            side_effect=[
                # First token: already expired (expires_in less than the buffer)
                httpx.Response(200, json={"access_token": "tok-1", "expires_in": 1}),
                httpx.Response(200, json={"access_token": "tok-2", "expires_in": 600}),
            ]
        )
        router.post("/api/search/product").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            await c.get_product_info("1")
            await c.get_product_info("2")

        assert token_ep.call_count == 2


async def test_login_failure_raises(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(401, text="nope")

        async with ShopwareClient(settings) as c:
            with pytest.raises(ShopwareAuthError, match="HTTP 401"):
                await c.get_product_info("1")


async def test_get_order_parses_line_items(settings):
    order_json = jsonlib.loads(SW_ORDER_FIXTURE.read_text())
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        post = router.post("/api/search/order").respond(200, json=order_json)

        async with ShopwareClient(settings) as c:
            order = await c.get_order("MK89643")

        assert order is not None
        assert order.order_number == "MK89643"
        assert len(order.line_items) == 5

        # Request body matches the documented filter + associations.
        body = jsonlib.loads(post.calls[0].request.content)
        assert body["filter"][0]["field"] == "orderNumber"
        assert body["filter"][0]["value"] == "MK89643"
        assert "product" in body["associations"]["lineItems"]["associations"]

        # The product line item carries the formerParentId we need to map.
        product_li = next(li for li in order.line_items if li.type == "product")
        assert product_li.product_id == "019290293e5871448b30d20d7ffc2a24"
        assert product_li.payload.product_number == "771883"
        assert (
            product_li.payload.dvsn_product_option_former_parent_id
            == "019ed4680e07739a8bda655a837f5cc2"
        )

        # Service options reference the same former parent.
        service_li = next(
            li for li in order.line_items if li.type == "dvsn-product-option"
        )
        assert (
            service_li.payload.dvsn_product_option_former_parent_id
            == "019ed4680e07739a8bda655a837f5cc2"
        )


async def test_get_order_returns_none_when_not_found(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/order").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            order = await c.get_order("UNKNOWN")

        assert order is None


async def test_bulk_fetch_keyed_by_product_number(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )

        # 300 is not found in Shopware → omitted from the result.
        mapping = {"100": ["a"], "200": ["b", "c"], "300": None}

        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            pn = body["filter"][0]["value"]
            cats = mapping[pn]
            data = [] if cats is None else [{"id": pn, "categoryIds": cats}]
            return httpx.Response(200, json={"data": data})

        router.post("/api/search/product").mock(side_effect=handler)

        async with ShopwareClient(settings) as c:
            result = await c.get_product_infos_bulk([100, 200, 300], concurrency=2)

        assert set(result) == {"100", "200"}
        assert result["100"].category_ids == ["a"]
        assert result["200"].category_ids == ["b", "c"]
