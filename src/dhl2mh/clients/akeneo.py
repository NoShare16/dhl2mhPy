"""Akeneo PIM REST API client: password-grant OAuth + product reads.

One client per PIM instance (MK = mykitchens, ML = mylivings); both share the
same username/password and differ only in base URL and client credentials.

Products are looked up by ``plenty_varianten_id``, the unique number attribute
carrying the Plenty variation id. Akeneo's number filter offers no ``IN``
operator, so a lookup is one request per article — bounded by a semaphore, the
same shape as ``ShopwareClient.get_product_infos_bulk``.

Errors are surfaced. The pipeline treats the whole enrichment as best-effort
(see ``pipeline._enrich_from_akeneo``) so a PIM outage costs the better product
name, not the run.
"""

import asyncio
import json
import time
from collections.abc import Iterable
from types import TracebackType
from typing import Any

import httpx
import structlog

from dhl2mh.config import AkeneoInstanceSettings, AkeneoSettings, Settings
from dhl2mh.mapping.constants import (
    AKENEO_COLOR_ATTRIBUTE,
    AKENEO_COLOR_PLACEHOLDER_CODES,
    AKENEO_LABEL_LOCALE,
    AKENEO_MODEL_ATTRIBUTE,
    AKENEO_VARIATION_ID_ATTRIBUTE,
)
from dhl2mh.models import AkeneoProduct, AkeneoProductInfo

log = structlog.get_logger()


class AkeneoAuthError(RuntimeError):
    pass


class AkeneoClient:
    """One Akeneo PIM instance. Use as ``async with``.

    The token carries an explicit TTL (``expires_in``, 3600s in both instances)
    and is renewed proactively a buffer ahead of expiry; a 401 also forces a
    refresh. Color option labels are cached per code for the client's lifetime —
    the attribute has ~500 options but a single run only ever touches a handful.
    """

    TOKEN_PATH = "/api/oauth/v1/token"
    PRODUCTS_PATH = "/api/rest/v1/products"
    OPTION_PATH = "/api/rest/v1/attributes/{attribute}/options/{code}"
    TOKEN_REFRESH_BUFFER_S = 60.0

    def __init__(
        self,
        name: str,
        instance: AkeneoInstanceSettings,
        credentials: AkeneoSettings,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._name = name
        self._instance = instance
        self._credentials = credentials
        self._client = httpx.AsyncClient(
            base_url=instance.base_url.rstrip("/"),
            timeout=timeout,
        )
        self._token: str | None = None
        self._token_expiry_monotonic: float = 0.0
        self._token_lock = asyncio.Lock()
        # option code → de_DE label; None marks a code Akeneo does not know.
        self._color_labels: dict[str, str | None] = {}

    @property
    def name(self) -> str:
        return self._name

    async def __aenter__(self) -> "AkeneoClient":
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

            log.info("akeneo.login", instance=self._name)
            resp = await self._client.post(
                self.TOKEN_PATH,
                auth=(self._instance.client_id, self._instance.secret),
                json={
                    "grant_type": "password",
                    "username": self._credentials.username,
                    "password": self._credentials.password,
                },
            )
            if resp.status_code != 200:
                raise AkeneoAuthError(
                    f"Akeneo {self._name} login failed: "
                    f"HTTP {resp.status_code} — {resp.text[:200]}"
                )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise AkeneoAuthError(
                    f"Akeneo {self._name} login: access_token missing — keys={list(data)}"
                )
            expires_in = float(data.get("expires_in", 3600))
            self._token = token
            self._token_expiry_monotonic = time.monotonic() + max(
                0.0, expires_in - self.TOKEN_REFRESH_BUFFER_S
            )
            log.info("akeneo.token_acquired", instance=self._name, expires_in=expires_in)
            return token

    async def _authed_get(self, path: str, *, params: Any = None) -> httpx.Response:
        token = await self._get_token()
        resp = await self._get_with_token(path, params, token)
        if resp.status_code == 401:
            log.warning("akeneo.token_expired", instance=self._name, path=path)
            token = await self._get_token(force_refresh=True)
            resp = await self._get_with_token(path, params, token)
        return resp

    async def _get_with_token(self, path: str, params: Any, token: str) -> httpx.Response:
        return await self._client.get(
            path,
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )

    # ── reads ───────────────────────────────────────────────────────────────

    async def get_product_info(
        self, plenty_variation_id: str | int
    ) -> AkeneoProductInfo | None:
        """Fetch one product by its Plenty variation id.

        Returns ``None`` when this instance does not know the variation. The
        color option code is resolved to its de_DE label before returning;
        placeholder options ("keine Angabe") count as no color at all.
        """
        vid = str(plenty_variation_id)
        search = {AKENEO_VARIATION_ID_ATTRIBUTE: [{"operator": "=", "value": vid}]}
        resp = await self._authed_get(
            self.PRODUCTS_PATH,
            params={
                "search": json.dumps(search),
                "attributes": f"{AKENEO_MODEL_ATTRIBUTE},{AKENEO_COLOR_ATTRIBUTE}",
                "limit": 1,
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Akeneo {self._name} product search for {vid} failed: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )
        items = (resp.json().get("_embedded") or {}).get("items") or []
        if not items:
            return None

        product = AkeneoProduct.model_validate(items[0])
        color_code = product.scalar(AKENEO_COLOR_ATTRIBUTE)
        if color_code in AKENEO_COLOR_PLACEHOLDER_CODES:
            color_code = None
        return AkeneoProductInfo(
            model=product.scalar(AKENEO_MODEL_ATTRIBUTE),
            color=await self._color_label(color_code) if color_code else None,
        )

    async def get_product_infos_bulk(
        self,
        plenty_variation_ids: Iterable[str | int],
        *,
        concurrency: int = 5,
    ) -> dict[str, AkeneoProductInfo]:
        """Fetch many products in parallel.

        Returns {variation_id_as_str: AkeneoProductInfo}; variations this
        instance does not know are omitted. Bounded by ``concurrency``.
        """
        sem = asyncio.Semaphore(concurrency)
        vids = [str(v) for v in plenty_variation_ids]

        async def fetch_one(vid: str) -> tuple[str, AkeneoProductInfo | None]:
            async with sem:
                return vid, await self.get_product_info(vid)

        results = await asyncio.gather(*(fetch_one(v) for v in vids))
        return {vid: info for vid, info in results if info is not None}

    async def _color_label(self, code: str) -> str:
        """de_DE label for a color option code, falling back to the code itself.

        An unknown code (404) is still better in the ProductName than nothing,
        so it is passed through verbatim and remembered so we ask only once.
        """
        if code in self._color_labels:
            return self._color_labels[code] or code

        resp = await self._authed_get(
            self.OPTION_PATH.format(attribute=AKENEO_COLOR_ATTRIBUTE, code=code)
        )
        label: str | None = None
        if resp.is_success:
            label = (resp.json().get("labels") or {}).get(AKENEO_LABEL_LOCALE)
        else:
            log.warning(
                "akeneo.color_option_unresolved",
                instance=self._name,
                code=code,
                status=resp.status_code,
            )
        self._color_labels[code] = label
        return label or code


class AkeneoProductLookup:
    """Product-name lookup spanning the configured PIM instances.

    Instances are queried in order (MK, then ML): an article is only looked up
    in the next instance when the previous one produced no usable name. Use as
    ``async with``.
    """

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self._clients = [
            AkeneoClient(name, instance, settings.akeneo, timeout=timeout)
            for name, instance in settings.akeneo_instances
        ]

    @property
    def enabled(self) -> bool:
        return bool(self._clients)

    async def __aenter__(self) -> "AkeneoProductLookup":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for client in self._clients:
            await client.__aexit__(exc_type, exc, tb)

    async def get_product_infos_bulk(
        self,
        plenty_variation_ids: Iterable[str | int],
        *,
        concurrency: int = 5,
    ) -> dict[str, AkeneoProductInfo]:
        """{variation_id_as_str: AkeneoProductInfo} for every article a PIM knows.

        Only entries carrying a ``modell`` are kept: a product without one
        yields no name, so the article stays pending for the next instance and
        ultimately falls back to the Shopware-derived name.
        """
        pending = [str(v) for v in plenty_variation_ids]
        found: dict[str, AkeneoProductInfo] = {}

        for client in self._clients:
            if not pending:
                break
            infos = await client.get_product_infos_bulk(pending, concurrency=concurrency)
            usable = {vid: info for vid, info in infos.items() if info.model}
            log.info(
                "akeneo.instance_matched",
                instance=client.name,
                requested=len(pending),
                matched=len(usable),
            )
            found.update(usable)
            pending = [vid for vid in pending if vid not in usable]

        return found
