import json as jsonlib

import httpx
import pytest
import respx

from dhl2mh.clients.shopware import ShopwareAuthError, ShopwareClient


async def test_get_categories_returns_ids(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        post = router.post("/api/search/product").respond(
            200, json={"data": [{"id": "p1", "categoryIds": ["cat-a", "cat-b"]}]}
        )

        async with ShopwareClient(settings) as c:
            cats = await c.get_categories("12345")

        assert cats == ["cat-a", "cat-b"]
        body = jsonlib.loads(post.calls[0].request.content)
        assert body["filter"][0]["field"] == "productNumber"
        assert body["filter"][0]["value"] == "12345"


async def test_get_categories_returns_empty_when_no_data(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            cats = await c.get_categories(99999)

        assert cats == []


async def test_search_failure_propagates(settings):
    """C# original swallowed errors and returned []. Python surfaces them."""
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(500, text="boom")

        async with ShopwareClient(settings) as c:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await c.get_categories("42")


async def test_token_cached_across_requests(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        token_ep = router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )
        router.post("/api/search/product").respond(200, json={"data": []})

        async with ShopwareClient(settings) as c:
            await c.get_categories("1")
            await c.get_categories("2")
            await c.get_categories("3")

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
            await c.get_categories("1")

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
            await c.get_categories("1")
            await c.get_categories("2")

        assert token_ep.call_count == 2


async def test_login_failure_raises(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(401, text="nope")

        async with ShopwareClient(settings) as c:
            with pytest.raises(ShopwareAuthError, match="HTTP 401"):
                await c.get_categories("1")


async def test_bulk_fetch_keyed_by_product_number(settings):
    with respx.mock(base_url=settings.shopware.base_url) as router:
        router.post("/api/oauth/token").respond(
            200, json={"access_token": "tok", "expires_in": 600}
        )

        mapping = {"100": ["a"], "200": ["b", "c"], "300": []}

        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            pn = body["filter"][0]["value"]
            return httpx.Response(
                200, json={"data": [{"id": pn, "categoryIds": mapping[pn]}]}
            )

        router.post("/api/search/product").mock(side_effect=handler)

        async with ShopwareClient(settings) as c:
            result = await c.get_categories_bulk([100, 200, 300], concurrency=2)

        assert result == mapping
