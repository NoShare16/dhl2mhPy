"""Smoke test for the full pipeline.

Mocks all three HTTP clients via respx, mocks smtplib for the skipped-orders
mail, and patches asyncio.sleep so the label-wait doesn't actually sleep.

Three orders flow through:
* fixture order (235655): 2 articles plus item 783174, which is NOT a whitelisted
  service id → treated as a non-service position and ignored, so the order
  uploads cleanly.
* a synthetic clean order (900001): single article, uploads + gets a label.
* a synthetic skip order (900002): a whitelisted service without a
  former_parent_id → skipped, lands in the report mail.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import respx
import structlog

from dhl2mh.mapping import SERVICE_AG
from dhl2mh.pipeline import run_pipeline

FIXTURE = Path(__file__).parent / "fixtures" / "plenty_order_bundle.json"

_SAMPLE_LABEL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<dsi:Transmission xmlns:dsi="http://www.it4logistics.de/i4ldata/ext">
  <Messages>
    <MessageContent>
      <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="ns6:OrderDocument">
        <OrderId><System>HDE</System><Id>900001</Id></OrderId>
        <OrderIdent>00340999900012345678</OrderIdent>
        <Document xsi:type="ns6:Label">
          <Barcode>00340999900012345678</Barcode>
        </Document>
      </ns6:Status>
    </MessageContent>
  </Messages>
</dsi:Transmission>
"""


def _synthetic_clean_order() -> dict:
    """A small, valid Plenty order with a single article — no service items,
    so it sails through the resolver without needing whitelisted service IDs."""
    return {
        "id": 900001,
        "statusId": 6.1,
        "typeId": 1,
        "createdAt": "2025-05-20T10:00:00+02:00",
        "relations": [
            {"orderId": 900001, "referenceType": "contact", "referenceId": 555,
             "relation": "receiver"},
        ],
        "addressRelations": [{"id": 1, "orderId": 900001, "typeId": 2, "addressId": 7}],
        "addresses": [
            {
                "id": 7,
                "name2": "Erika",
                "name3": "Beispiel",
                "address1": "Teststr. 1",
                "address2": "",
                "postalCode": "12345",
                "town": "Hannover",
                "countryId": 1,
                "options": [{"typeId": 5, "value": "erika@example.com"}],
            }
        ],
        "orderItems": [
            {
                "typeId": 1,
                "itemVariationId": 5050,
                "orderItemName": "Standalone-Artikel",
                "quantity": Decimal(1),
                "properties": [],
                "variation": {
                    "stockLimitation": 0,
                    "weightG": 10000,
                    "widthMM": 500,
                    "lengthMM": 400,
                    "heightMM": 300,
                },
            }
        ],
        "properties": [{"typeId": 7, "value": "SW-XYZ"}],
        "shippingPackages": [],
    }


def _synthetic_skip_order() -> dict:
    """Article + a real (whitelisted) service that has no former_parent_id —
    no Plenty property 1021 and no shopware_id to enrich from — so the order is
    skipped by the mandatory-former-parent rule and lands in the report mail."""
    return {
        "id": 900002,
        "statusId": 6.1,
        "typeId": 1,
        "createdAt": "2025-05-20T10:00:00+02:00",
        "relations": [
            {"orderId": 900002, "referenceType": "contact", "referenceId": 556,
             "relation": "receiver"},
        ],
        "addressRelations": [{"id": 1, "orderId": 900002, "typeId": 2, "addressId": 8}],
        "addresses": [
            {
                "id": 8,
                "name2": "Max",
                "name3": "Muster",
                "address1": "Weg 2",
                "address2": "",
                "postalCode": "54321",
                "town": "Bremen",
                "countryId": 1,
                "options": [{"typeId": 5, "value": "max@example.com"}],
            }
        ],
        "orderItems": [
            {
                "typeId": 1,
                "itemVariationId": 6060,
                "orderItemName": "Artikel mit Service",
                "quantity": Decimal(1),
                "properties": [],
                "variation": {"stockLimitation": 0, "weightG": 10000,
                              "widthMM": 500, "lengthMM": 400, "heightMM": 300},
            },
            {
                "typeId": 1,
                "itemVariationId": SERVICE_AG,  # whitelisted service, no former_parent
                "orderItemName": "Altgerätemitnahme",
                "quantity": Decimal(1),
                "properties": [],
                "variation": {"stockLimitation": 2},
            },
        ],
        "properties": [],  # no shopware_id → manual order, no enrichment
        "shippingPackages": [],
    }


def _to_jsonable(obj):
    """Walk the dict and convert Decimal → str so respx can json-encode it."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    return obj


async def test_pipeline_smoke_runs_end_to_end(settings):
    fixture_order = json.loads(FIXTURE.read_text())
    synthetic = _synthetic_clean_order()
    skip_order = _synthetic_skip_order()
    orders_page = {
        "isLastPage": True,
        "entries": [
            fixture_order,
            _to_jsonable(synthetic),
            _to_jsonable(skip_order),
        ],
    }

    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client

        # Plenty
        plenty_base = settings.plenty.base_url
        router.post(f"{plenty_base}/rest/login").respond(
            200, json={"access_token": "plenty-tok"}
        )
        router.get(f"{plenty_base}/rest/orders/shipping/countries").respond(
            200, json=[{"id": 1, "isoCode2": "DE"}]
        )
        router.get(f"{plenty_base}/rest/orders/search").respond(200, json=orders_page)
        plenty_push = router.post(
            f"{plenty_base}/rest/orders/900001/shipping/packages"
        ).respond(200, json={})

        # Shopware
        sw_base = settings.shopware.base_url
        router.post(f"{sw_base}/api/oauth/token").respond(
            200, json={"access_token": "sw-tok", "expires_in": 600}
        )
        router.post(f"{sw_base}/api/search/product").respond(
            200, json={"data": []}  # no categories needed for these orders
        )
        router.post(f"{sw_base}/api/search/order").respond(
            200, json={"data": []}  # no SW order → keep the Plenty-seeded value
        )

        # DHL
        dhl_base = settings.dhl_base_url
        dhl_upload = router.post(
            f"{dhl_base}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")
        router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_SAMPLE_LABEL_XML
        )

        summary = await run_pipeline(settings)

    # 3 fetched: fixture order (783174 ignored) + synthetic clean order both
    # upload; the synthetic skip order is dropped for a missing former_parent_id.
    assert summary.fetched == 3
    assert summary.uploaded == 2
    assert summary.labels_received == 1
    assert summary.tracking_pushed == 1
    assert summary.skipped == 1

    assert dhl_upload.call_count == 2
    assert plenty_push.call_count == 1

    # Pushed payload carries the OrderIdent from the label
    pushed_body = plenty_push.calls[0].request.content.decode()
    assert "00340999900012345678" in pushed_body

    # Skipped report mail was sent
    smtp_cls.assert_called_once()
    msg = smtp_client.send_message.call_args[0][0]
    assert "1 Order(s) übersprungen" in msg["Subject"]


async def test_pipeline_dry_run_uploads_but_skips_plenty_and_mail(settings):
    fixture_order = json.loads(FIXTURE.read_text())
    synthetic = _synthetic_clean_order()
    orders_page = {
        "isLastPage": True,
        "entries": [fixture_order, _to_jsonable(synthetic)],
    }

    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        plenty_base = settings.plenty.base_url
        router.post(f"{plenty_base}/rest/login").respond(
            200, json={"access_token": "plenty-tok"}
        )
        router.get(f"{plenty_base}/rest/orders/shipping/countries").respond(
            200, json=[{"id": 1, "isoCode2": "DE"}]
        )
        router.get(f"{plenty_base}/rest/orders/search").respond(200, json=orders_page)
        plenty_push = router.post(
            f"{plenty_base}/rest/orders/900001/shipping/packages"
        ).respond(200, json={})

        sw_base = settings.shopware.base_url
        router.post(f"{sw_base}/api/oauth/token").respond(
            200, json={"access_token": "sw-tok", "expires_in": 600}
        )
        router.post(f"{sw_base}/api/search/product").respond(200, json={"data": []})
        router.post(f"{sw_base}/api/search/order").respond(200, json={"data": []})

        dhl_base = settings.dhl_base_url
        dhl_upload = router.post(
            f"{dhl_base}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")
        router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_SAMPLE_LABEL_XML
        )

        summary = await run_pipeline(settings, dry_run=True)

    # DHL UAT upload + label pull still happen (both orders upload)
    assert dhl_upload.call_count == 2
    assert summary.uploaded == 2
    assert summary.labels_received == 1
    # but nothing is written back to Plenty and no mail is sent
    assert summary.tracking_pushed == 0
    assert plenty_push.call_count == 0
    smtp_cls.assert_not_called()


async def test_pipeline_logs_per_order_skip_reason_and_missing_labels(settings):
    """The cron log must name each dropped/unlabelled order, not just counts.

    The synthetic skip order (900002) is dropped for a missing former_parent_id
    and the fixture order is uploaded but gets no label back (the status XML only
    carries 900001), so it must surface as ``pipeline.labels_missing``.
    """
    fixture_order = json.loads(FIXTURE.read_text())
    orders_page = {
        "isLastPage": True,
        "entries": [
            fixture_order,
            _to_jsonable(_synthetic_clean_order()),
            _to_jsonable(_synthetic_skip_order()),
        ],
    }

    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        plenty_base = settings.plenty.base_url
        router.post(f"{plenty_base}/rest/login").respond(
            200, json={"access_token": "plenty-tok"}
        )
        router.get(f"{plenty_base}/rest/orders/shipping/countries").respond(
            200, json=[{"id": 1, "isoCode2": "DE"}]
        )
        router.get(f"{plenty_base}/rest/orders/search").respond(200, json=orders_page)
        router.post(
            f"{plenty_base}/rest/orders/900001/shipping/packages"
        ).respond(200, json={})

        sw_base = settings.shopware.base_url
        router.post(f"{sw_base}/api/oauth/token").respond(
            200, json={"access_token": "sw-tok", "expires_in": 600}
        )
        router.post(f"{sw_base}/api/search/product").respond(200, json={"data": []})
        router.post(f"{sw_base}/api/search/order").respond(200, json={"data": []})

        dhl_base = settings.dhl_base_url
        router.post(f"{dhl_base}/transmission/{settings.dhl_username}").respond(
            200, text="<Ack/>"
        )
        router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_SAMPLE_LABEL_XML
        )

        await run_pipeline(settings)

    # 900002 is skipped → its order_id + reason are logged (not just a count)
    skip_events = [e for e in logs if e["event"] == "pipeline.order_skipped"]
    assert any(e["order_id"] == 900002 for e in skip_events)

    # Both uploads name their order_id; the aggregate carries the id list
    uploaded = next(e for e in logs if e["event"] == "pipeline.uploaded")
    assert 900001 in uploaded["order_ids"]

    # The fixture order uploaded but got no label back → reconciliation warning
    missing = next(e for e in logs if e["event"] == "pipeline.labels_missing")
    assert 900001 not in missing["order_ids"]  # 900001 did get a label
    assert missing["count"] == len(missing["order_ids"]) >= 1


async def _no_sleep(_seconds):
    return None


async def test_pipeline_with_no_orders_sends_no_mail_and_does_not_upload(settings):
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        router.post(f"{settings.plenty.base_url}/rest/login").respond(
            200, json={"access_token": "tok"}
        )
        router.get(
            f"{settings.plenty.base_url}/rest/orders/shipping/countries"
        ).respond(200, json=[])
        router.get(f"{settings.plenty.base_url}/rest/orders/search").respond(
            200, json={"isLastPage": True, "entries": []}
        )
        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200)

        summary = await run_pipeline(settings)

    assert summary.fetched == 0
    assert summary.uploaded == 0
    assert summary.skipped == 0
    assert dhl_upload.call_count == 0
    smtp_cls.assert_not_called()
