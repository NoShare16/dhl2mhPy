"""Plenty REST client: auth, list orders/countries, push tracking ident back."""

import asyncio
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import httpx
import structlog

from dhl2mh.config import Settings
from dhl2mh.models import ApiCountry, ApiOrder, ApiOrderPage, PackageData

log = structlog.get_logger()


class PlentyAuthError(RuntimeError):
    pass


class PlentyClient:
    """One client per workflow run. Use as ``async with``.

    Token is fetched lazily and refreshed once on 401. Plenty issues 24h tokens
    but we don't trust the lifetime — 401-retry is the source of truth.
    """

    LOGIN_PATH = "/rest/login"
    COUNTRIES_PATH = "/rest/orders/shipping/countries"
    ORDERS_SEARCH_PATH = "/rest/orders/search"

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.plenty.base_url,
            timeout=timeout,
        )
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "PlentyClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    # ── auth ────────────────────────────────────────────────────────────────

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token

        async with self._token_lock:
            if self._token and not force_refresh:
                return self._token

            log.info("plenty.login")
            resp = await self._client.post(
                self.LOGIN_PATH,
                json={
                    "username": self._settings.plenty.username,
                    "password": self._settings.plenty.password,
                },
            )
            if resp.status_code != 200:
                raise PlentyAuthError(
                    f"Plenty login failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise PlentyAuthError(
                    f"Plenty login: access_token missing — keys={list(data)}"
                )
            self._token = token
            return token

    async def _authed_request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> httpx.Response:
        token = await self._get_token()
        resp = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            log.warning("plenty.token_expired", path=path)
            token = await self._get_token(force_refresh=True)
            resp = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        return resp

    # ── reads ───────────────────────────────────────────────────────────────

    async def get_countries(self) -> dict[int, str]:
        """{country_id: iso_code2} for every country that has an ISO code."""
        resp = await self._authed_request("GET", self.COUNTRIES_PATH)
        resp.raise_for_status()
        countries = [ApiCountry.model_validate(c) for c in resp.json()]
        return {c.id: c.iso_code2 for c in countries if c.iso_code2}

    async def iter_orders(self, items_per_page: int = 50) -> AsyncIterator[ApiOrder]:
        """Stream orders across pages — mirrors the C# query string exactly."""
        page = 1
        while True:
            # Tuples preserve order and let us repeat the with[] key.
            params: list[tuple[str, Any]] = [
                ("lazyLoaded", "false"),
                ("orderProperty_2", 26),
                ("statusId", 6.1),
                ("with[]", "shippingPackages"),
                ("with[]", "addresses"),
                ("with[]", "orderItems.variation"),
                ("page", page),
                ("itemsPerPage", items_per_page),
            ]
            resp = await self._authed_request("GET", self.ORDERS_SEARCH_PATH, params=params)
            resp.raise_for_status()
            result = ApiOrderPage.model_validate(resp.json())
            log.info(
                "plenty.orders_page",
                page=page,
                count=len(result.entries),
                is_last=result.is_last_page,
            )
            for order in result.entries:
                yield order
            if result.is_last_page:
                return
            page += 1

    # ── writes ──────────────────────────────────────────────────────────────

    async def update_package(self, order_id: int, package: PackageData) -> None:
        """Push tracking number back to Plenty. Raises on any non-2xx."""
        resp = await self._authed_request(
            "POST",
            f"/rest/orders/{order_id}/shipping/packages",
            json={
                "packageId": package.package_id,
                "packageNumber": package.package_number,
                "packageType": package.package_type,
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Plenty package update for order {order_id} failed: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )
        log.info(
            "plenty.package_updated",
            order_id=order_id,
            package_number=package.package_number,
        )
