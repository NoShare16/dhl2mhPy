"""Shopware 6 Admin API client: OAuth client_credentials + product categories."""

import asyncio
import time
from collections.abc import Iterable
from types import TracebackType
from typing import Any

import httpx
import structlog

from dhl2mh.config import Settings

log = structlog.get_logger()


class ShopwareAuthError(RuntimeError):
    pass


class ShopwareClient:
    """One client per workflow run. Use as ``async with``.

    Token has an explicit TTL (Shopware returns ``expires_in``) and is renewed
    proactively a buffer ahead of expiry. A 401 also forces a refresh.

    Errors are surfaced (unlike the C# original which swallowed them and
    returned an empty category list — that masked failures in the filter stage).
    """

    TOKEN_PATH = "/api/oauth/token"
    SEARCH_PRODUCT_PATH = "/api/search/product"
    TOKEN_REFRESH_BUFFER_S = 60.0

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.shopware.base_url,
            timeout=timeout,
        )
        self._token: str | None = None
        self._token_expiry_monotonic: float = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "ShopwareClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    # ── auth ────────────────────────────────────────────────────────────────

    def _token_is_valid(self) -> bool:
        return self._token is not None and time.monotonic() < self._token_expiry_monotonic

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._token_is_valid():
            return self._token  # type: ignore[return-value]

        async with self._token_lock:
            if not force_refresh and self._token_is_valid():
                return self._token  # type: ignore[return-value]

            log.info("shopware.login")
            resp = await self._client.post(
                self.TOKEN_PATH,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.shopware.client_id,
                    "client_secret": self._settings.shopware.client_secret,
                },
            )
            if resp.status_code != 200:
                raise ShopwareAuthError(
                    f"Shopware login failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise ShopwareAuthError(
                    f"Shopware login: access_token missing — keys={list(data)}"
                )
            expires_in = float(data.get("expires_in", 600))
            self._token = token
            self._token_expiry_monotonic = time.monotonic() + max(
                0.0, expires_in - self.TOKEN_REFRESH_BUFFER_S
            )
            log.info("shopware.token_acquired", expires_in=expires_in)
            return token

    async def _authed_post(self, path: str, *, json: Any) -> httpx.Response:
        token = await self._get_token()
        resp = await self._post_with_token(path, json, token)
        if resp.status_code == 401:
            log.warning("shopware.token_expired", path=path)
            token = await self._get_token(force_refresh=True)
            resp = await self._post_with_token(path, json, token)
        return resp

    async def _post_with_token(self, path: str, payload: Any, token: str) -> httpx.Response:
        return await self._client.post(
            path,
            json=payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "sw-access-key": token,
            },
        )

    # ── reads ───────────────────────────────────────────────────────────────

    async def get_categories(self, product_number: str | int) -> list[str]:
        """Category IDs for a single product (by productNumber). Empty list if not found."""
        pn = str(product_number)
        body = {
            "filter": [{"type": "equals", "field": "productNumber", "value": pn}],
            "associations": {"categories": {}},
        }
        resp = await self._authed_post(self.SEARCH_PRODUCT_PATH, json=body)
        if not resp.is_success:
            raise RuntimeError(
                f"Shopware product search for {pn} failed: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )
        data = resp.json().get("data") or []
        if not data:
            return []
        return [str(cid) for cid in (data[0].get("categoryIds") or [])]

    async def get_categories_bulk(
        self,
        product_numbers: Iterable[str | int],
        *,
        concurrency: int = 5,
    ) -> dict[str, list[str]]:
        """Fetch categories for many products in parallel.

        Returns {product_number_as_str: [category_ids]}. Bounded by ``concurrency``
        to avoid hammering Shopware (default 5 in-flight at a time).
        """
        sem = asyncio.Semaphore(concurrency)
        pns = [str(pn) for pn in product_numbers]

        async def fetch_one(pn: str) -> tuple[str, list[str]]:
            async with sem:
                return pn, await self.get_categories(pn)

        results = await asyncio.gather(*(fetch_one(pn) for pn in pns))
        return dict(results)
