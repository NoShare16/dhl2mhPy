"""DHL DeliverIT client: XML upload + label-status pull (tracking ident only)."""

import base64
import hashlib
from datetime import datetime
from pathlib import Path
from types import TracebackType

import httpx
import structlog
from lxml import etree

from dhl2mh.config import Settings
from dhl2mh.models import AckError, LabelInfo

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
        self._ack_read_timeout = settings.dhl.ack_read_timeout_seconds
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
        creds = f"{username}:{sha1_hex_upper}".encode()
        return "Basic " + base64.b64encode(creds).decode("ascii")

    # ── upload ──────────────────────────────────────────────────────────────

    async def upload_order_xml(
        self, xml_bytes: bytes, *, order_id: int | None = None
    ) -> list[AckError]:
        """POST one order's XML with ``?ack=true``. Returns the rejections.

        An empty list means DHL accepted the order. Raises only on non-2xx —
        a *rejected* order still answers HTTP 200, which is exactly why this
        goes out with ``ack=true``: without it the body is empty and the
        rejection surfaces nowhere until someone drains the (consume-once)
        acknowledgement queue.

        ``order_id`` is logged only — it lets the cron log say *which* order was
        transmitted.
        """
        resp = await self._client.post(
            f"/transmission/{self._mandant}",
            params={"ack": "true"},
            content=xml_bytes,
            headers={
                "Authorization": self._auth_header,
                "Content-Type": "text/xml; charset=utf-8",
            },
        )
        if not resp.is_success:
            raise RuntimeError(
                f"DHL upload failed (order {order_id}): "
                f"HTTP {resp.status_code} — {resp.text[:300]}"
            )

        errors = self._parse_ack_errors(resp.content)
        for err in errors:
            log.error(
                "dhl.upload_rejected",
                order_id=err.order_id or order_id,
                error_code=err.error_code,
                error_text=err.error_text,
            )
        if not errors:
            log.info(
                "dhl.uploaded", order_id=order_id, status=resp.status_code, size=len(xml_bytes)
            )
        return errors

    # ── acknowledgement queue ───────────────────────────────────────────────

    async def fetch_acknowledgements(self, archive_dir: Path) -> tuple[Path, list[AckError]]:
        """Drain the acknowledgement queue. Returns (archived raw file, errors).

        **Consume-once**: DHL clears the queue for this Mandant once it answers,
        so the response is streamed straight to disk before anything is parsed.
        A parser bug then costs nothing — the raw answer is still on disk. The
        read timeout is deliberately generous for the same reason: a timeout
        drains the queue server-side regardless, and the content would be gone.
        """
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / (
            f"acknowledgement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
        )

        written = 0
        async with self._client.stream(
            "GET",
            f"/transmissionAcknowledgement/{self._mandant}",
            headers={"Authorization": self._auth_header},
            timeout=httpx.Timeout(60.0, read=self._ack_read_timeout),
        ) as resp:
            if not resp.is_success:
                await resp.aread()
                raise RuntimeError(
                    f"DHL transmissionAcknowledgement failed: "
                    f"HTTP {resp.status_code} — {resp.text[:300]}"
                )
            with target.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
                    written += len(chunk)

        log.info("dhl.acknowledgement_archived", path=str(target), bytes=written)

        if not written:
            return target, []  # empty queue — nothing was pending

        errors = self._parse_ack_errors(target.read_bytes())
        log.info("dhl.acknowledgement_parsed", errors=len(errors))
        return target, errors

    @staticmethod
    def _parse_ack_errors(xml_bytes: bytes) -> list[AckError]:
        """Pick the rejected orders out of a TransmissionAcknowledgement.

        Every transmitted order gets an ``AcknowledgementDetails`` block that
        echoes it back, so the block's presence says nothing. Only a block
        carrying ``ErrorCode`` is a rejection.
        """
        if not xml_bytes.strip():
            return []
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            log.warning("dhl.ack_unparseable", error=str(e))
            return []

        errors: list[AckError] = []
        for details in root.iter("{*}AcknowledgementDetails"):
            code = _direct_text(details, "ErrorCode")
            if not code:
                continue
            errors.append(
                AckError(
                    order_id=_ack_order_id(details),
                    error_code=code,
                    error_text=_direct_text(details, "ErrorResponse"),
                )
            )
        return errors

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

    async def get_labels_for_order(self, order_id: int) -> list[LabelInfo]:
        """transmissionStatus for a single order — does *not* drain the queue.

        ``get_labels`` consumes the collective queue for the whole Mandant, so
        a label pulled by one run is gone for every other. Filtering by
        ``orderId={System}_{Id}`` answers for one order without touching the
        rest, which is what you want when investigating a specific order.
        """
        resp = await self._client.get(
            f"/transmissionStatus/{self._mandant}",
            params={"orderId": f"{self._mandant}_{order_id}"},
            headers={"Authorization": self._auth_header},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"DHL transmissionStatus failed (order {order_id}): "
                f"HTTP {resp.status_code} — {resp.text[:300]}"
            )
        labels = self._dedupe_by_order(self._parse_label_xml(resp.content))
        log.info("dhl.labels_pulled_for_order", order_id=order_id, count=len(labels))
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


def _ack_order_id(details) -> int | None:
    """Our Plenty order id, echoed back under Message/MessageContent/Order.

    Returns None when the rejection carries no readable order id — a report
    entry without an id still beats dropping the error.
    """
    for order_id_el in details.iter("{*}OrderId"):
        text = _direct_text(order_id_el, "Id")
        try:
            return int(text)
        except ValueError:
            log.warning("dhl.ack_non_int_orderid", order_id=text)
            return None
    return None
