import httpx
import pytest
import respx

from dhl2mh.clients.plenty import PlentyAuthError, PlentyClient
from dhl2mh.models import PackageData


async def test_login_caches_token_across_requests(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        login = router.post("/rest/login").respond(200, json={"access_token": "tok-1"})
        router.get("/rest/orders/shipping/countries").respond(
            200, json=[{"id": 1, "isoCode2": "DE"}]
        )

        async with PlentyClient(settings) as client:
            await client.get_countries()
            await client.get_countries()

        assert login.call_count == 1  # token reused


async def test_get_countries_returns_id_to_iso_map(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(200, json={"access_token": "tok"})
        router.get("/rest/orders/shipping/countries").respond(
            200,
            json=[
                {"id": 1, "isoCode2": "DE"},
                {"id": 2, "isoCode2": "AT"},
                {"id": 3, "isoCode2": None},  # filtered out
            ],
        )

        async with PlentyClient(settings) as client:
            countries = await client.get_countries()

        assert countries == {1: "DE", 2: "AT"}


async def test_iter_orders_walks_pages_until_is_last(settings):
    page1 = {
        "isLastPage": False,
        "entries": [
            {
                "id": 1,
                "statusId": 6.1,
                "typeId": 1,
                "createdAt": "2025-01-15T10:00:00+00:00",
                "orderItems": [],
            }
        ],
    }
    page2 = {
        "isLastPage": True,
        "entries": [
            {
                "id": 2,
                "statusId": 6.1,
                "typeId": 1,
                "createdAt": "2025-01-15T11:00:00+00:00",
                "orderItems": [],
            }
        ],
    }

    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(200, json={"access_token": "tok"})
        search = router.get("/rest/orders/search")
        search.mock(side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])

        async with PlentyClient(settings) as client:
            ids = [o.id async for o in client.iter_orders(items_per_page=10)]

        assert ids == [1, 2]
        assert search.call_count == 2


async def test_iter_orders_query_string_matches_csharp(settings):
    """Same params the C# PlentyGetOrders builds — Plenty filters strictly on these."""
    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(200, json={"access_token": "tok"})
        search = router.get("/rest/orders/search").respond(
            200, json={"isLastPage": True, "entries": []}
        )

        async with PlentyClient(settings) as client:
            _ = [o async for o in client.iter_orders(items_per_page=10)]

        sent_url = str(search.calls[0].request.url)
        # repeated with[] keys
        assert sent_url.count("with%5B%5D=") == 3
        assert "shippingPackages" in sent_url
        assert "addresses" in sent_url
        assert "orderItems.variation" in sent_url
        assert "statusId=6.1" in sent_url
        assert "page=1" in sent_url
        assert "itemsPerPage=10" in sent_url


async def test_401_triggers_token_refresh_and_retry(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        login = router.post("/rest/login")
        login.mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "tok-old"}),
                httpx.Response(200, json={"access_token": "tok-new"}),
            ]
        )
        get = router.get("/rest/orders/shipping/countries")
        get.mock(side_effect=[httpx.Response(401), httpx.Response(200, json=[])])

        async with PlentyClient(settings) as client:
            await client.get_countries()

        assert login.call_count == 2
        assert get.call_count == 2
        assert get.calls[-1].request.headers["Authorization"] == "Bearer tok-new"


async def test_login_failure_raises(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(403, text="forbidden")

        async with PlentyClient(settings) as client:
            with pytest.raises(PlentyAuthError, match="HTTP 403"):
                await client.get_countries()


async def test_update_package_posts_correct_payload(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(200, json={"access_token": "tok"})
        post = router.post("/rest/orders/12345/shipping/packages").respond(200, json={})

        async with PlentyClient(settings) as client:
            await client.update_package(
                12345,
                PackageData(package_number="DHL-IDENT-123"),
            )

        body = post.calls[0].request.content.decode()
        assert '"packageId":1' in body or '"packageId": 1' in body
        assert "DHL-IDENT-123" in body
        assert '"packageType":0' in body or '"packageType": 0' in body


async def test_update_package_raises_on_failure(settings):
    with respx.mock(base_url=settings.plenty.base_url) as router:
        router.post("/rest/login").respond(200, json={"access_token": "tok"})
        router.post("/rest/orders/42/shipping/packages").respond(500, text="boom")

        async with PlentyClient(settings) as client:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await client.update_package(42, PackageData(package_number="x"))
