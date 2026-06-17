"""DHL DeliverIT client: XML upload + label-status pull (tracking ident only)."""

import base64
import hashlib
from types import TracebackType

import httpx
import structlog
from lxml import etree

from dhl2mh.config import Settings
from dhl2mh.models import LabelInfo

log = structlog.get_logger()

DSI_NS = "http://www.it4logistics.de/i4ldata/ext"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


class DhlClient:
    """One client per workflow run. Use as ``async with``.

    Basic Auth with SHA1-hashed (uppercase hex) password — that's DHL's contract,
    not a security choice. Same auth works for transmission (POST XML) and
    transmissionStatus (GET XML).
    """

    def __init__(self, settings: Settings, *, timeout: float = 60.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.dhl_base_url,
            timeout=timeout,
        )
        self._mandant = settings.dhl_username  # DHL term: "Mandantenkürzel"
        self._auth_header = self._build_basic_auth(
            settings.dhl_username, settings.dhl_password
        )

    async def __aenter__(self) -> "DhlClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    @staticmethod
    def _build_basic_auth(username: str, password: str) -> str:
        sha1_hex_upper = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        creds = f"{username}:{sha1_hex_upper}".encode("utf-8")
        return "Basic " + base64.b64encode(creds).decode("ascii")

    # ── upload ──────────────────────────────────────────────────────────────

    async def upload_order_xml(self, xml_bytes: bytes) -> str:
        """POST one order's XML. Returns response body. Raises on non-2xx."""
        resp = await self._client.post(
            f"/transmission/{self._mandant}",
            content=xml_bytes,
            headers={
                "Authorization": self._auth_header,
                "Content-Type": "text/xml; charset=utf-8",
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"DHL upload failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
        log.info("dhl.uploaded", status=resp.status_code, size=len(xml_bytes))
        return resp.text

    # ── label status ────────────────────────────────────────────────────────

    async def get_labels(self) -> list[LabelInfo]:
        """Pull transmissionStatus, return tracking idents from Label documents.

        PDF content in the response is ignored — we only push ``OrderIdent``
        back to Plenty.
        """
        resp = await self._client.get(
            f"/transmissionStatus/{self._mandant}",
            headers={"Authorization": self._auth_header},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"DHL transmissionStatus failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
        labels = self._dedupe_by_order(self._parse_label_xml(resp.content))
        log.info("dhl.labels_pulled", count=len(labels))
        return labels

    @staticmethod
    def _dedupe_by_order(labels: list[LabelInfo]) -> list[LabelInfo]:
        """One tracking ident per order — there are no multi-package shipments,
        and the status response can repeat a Status block for the same order."""
        by_order: dict[int, LabelInfo] = {}
        for label in labels:
            by_order.setdefault(label.order_id, label)
        return list(by_order.values())

    @staticmethod
    def _parse_label_xml(xml_bytes: bytes) -> list[LabelInfo]:
        """Walk all Messages → keep Status of type OrderDocument with Document of type Label."""
        root = etree.fromstring(xml_bytes)
        labels: list[LabelInfo] = []

        for messages in root.iter("{*}Messages"):
            content = next((c for c in messages if _localname(c) == "MessageContent"), None)
            if content is None:
                continue
            for status in content:
                if _localname(status) != "Status":
                    continue
                if not _xsi_type(status).endswith("OrderDocument"):
                    continue

                document = next(
                    (c for c in status if _localname(c) == "Document"),
                    None,
                )
                if document is None or not _xsi_type(document).endswith("Label"):
                    continue

                order_id_text = _child_text(_child(status, "OrderId"), "Id")
                ident_text = _direct_text(status, "OrderIdent")
                barcode_text = _direct_text(document, "Barcode")

                if not order_id_text or not ident_text:
                    log.warning(
                        "dhl.label_incomplete",
                        order_id=order_id_text,
                        has_ident=bool(ident_text),
                    )
                    continue

                try:
                    order_id_int = int(order_id_text)
                except ValueError:
                    log.warning("dhl.label_non_int_orderid", order_id=order_id_text)
                    continue

                labels.append(
                    LabelInfo(
                        order_id=order_id_int,
                        order_ident=ident_text,
                        barcode=barcode_text,
                    )
                )
        return labels


# ── small XML helpers (namespace-agnostic) ─────────────────────────────────


def _localname(el) -> str:
    return etree.QName(el).localname


def _xsi_type(el) -> str:
    return el.get(f"{{{XSI_NS}}}type") or ""


def _child(parent, local: str):
    if parent is None:
        return None
    return next((c for c in parent if _localname(c) == local), None)


def _direct_text(parent, local: str) -> str:
    el = _child(parent, local)
    return (el.text or "").strip() if el is not None and el.text else ""


def _child_text(parent, local: str) -> str:
    return _direct_text(parent, local)
