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
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import respx
import structlog

from dhl2mh.mapping.constants import COLOR_GROUP_ID, SECOND_CHOICE_TAG_ID, SERVICE_AG
from dhl2mh.pipeline import LABEL_MISSING_REASON, run_pipeline
from tests.paths import FIXTURES

FIXTURE = FIXTURES / "plenty_order_bundle.json"

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


# A status answer without any label — what a rejected order really gets.
_EMPTY_STATUS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<dsi:Transmission xmlns:dsi="http://www.it4logistics.de/i4ldata/ext"/>
"""

# An empty queue: DHL answers with the envelope and no AcknowledgementDetails.
_EMPTY_ACK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ns6:TransmissionAcknowledgement xmlns:ns6="http://www.it4logistics.de/i4ldata/ext">
  <SendingPartyID>DELIVERIT</SendingPartyID>
  <ReceivingPartyID>HDE</ReceivingPartyID>
</ns6:TransmissionAcknowledgement>
"""


def _rejection_ack(order_id: int, code: str, text: str = "abgelehnt") -> bytes:
    """A TransmissionAcknowledgement rejecting one order, shaped like the real
    DHL answer: ErrorResponse/ErrorCode sit next to the echoed order."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ns6:TransmissionAcknowledgement xmlns:ns6="http://www.it4logistics.de/i4ldata/ext">
  <SendingPartyID>DELIVERIT</SendingPartyID>
  <ReceivingPartyID>HDE</ReceivingPartyID>
  <AcknowledgementDetails>
    <ErrorResponse>{text}</ErrorResponse>
    <ErrorCode>{code}</ErrorCode>
    <Message>
      <MessageContent>
        <ns6:Order>
          <OrderId><System>HDE</System><Id>{order_id}</Id></OrderId>
          <OrderNr>{order_id}</OrderNr>
        </ns6:Order>
      </MessageContent>
    </Message>
  </AcknowledgementDetails>
</ns6:TransmissionAcknowledgement>
""".encode()


def _synthetic_clean_order(variation_number: str | None = None) -> dict:
    """A small, valid Plenty order with a single article — no service items,
    so it sails through the resolver without needing whitelisted service IDs.

    ``variation_number`` fills the Plenty "Variantennummer", the name source for
    second-choice articles.
    """
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
                    "number": variation_number,
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


def _sw_product_handler(request: httpx.Request) -> httpx.Response:
    """Shopware product search: every article gets manufacturerNumber + color.

    Without this the articles would carry no model name from either source and
    the new ``require_model_names`` gate would skip every order — these tests
    are about the rest of the pipeline, so they need the fallback to work.
    """
    return _sw_product_response(request, tag_ids=[])


def _sw_b_ware_product_handler(request: httpx.Request) -> httpx.Response:
    """Same product, but tagged "B-Ware" — the article is second choice."""
    return _sw_product_response(request, tag_ids=["irgendein-tag", SECOND_CHOICE_TAG_ID])


def _sw_b_ware_unnamed_product_handler(request: httpx.Request) -> httpx.Response:
    """B-Ware tag, but no manufacturerNumber/color — Shopware yields no name."""
    return _sw_product_response(
        request, tag_ids=[SECOND_CHOICE_TAG_ID], with_name=False
    )


def _sw_product_response(
    request: httpx.Request, *, tag_ids: list[str], with_name: bool = True
) -> httpx.Response:
    pn = json.loads(request.content)["filter"][0]["value"]
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": pn,
                    "productNumber": pn,
                    "manufacturerNumber": f"MN-{pn}" if with_name else None,
                    "categoryIds": [],
                    "tagIds": tag_ids,
                    "properties": (
                        [{"name": "Schwarz", "groupId": COLOR_GROUP_ID}]
                        if with_name
                        else []
                    ),
                }
            ]
        },
    )


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
    fixture_order = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
        router.post(f"{sw_base}/api/search/product").mock(side_effect=_sw_product_handler)
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
        router.get(
            f"{dhl_base}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=_EMPTY_ACK_XML)

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

    # Report mail covers both the filter-skipped order (900002) and the order
    # that uploaded but got no label back (235655, the fixture order).
    smtp_cls.assert_called_once()
    msg = smtp_client.send_message.call_args[0][0]
    assert "2 Order(s) benötigen Prüfung" in msg["Subject"]
    body = msg.get_content()
    assert "900002" in body
    assert "235655" in body
    assert "kein Label" in body  # the missing-label reason text


async def test_pipeline_dry_run_uploads_but_skips_plenty_and_mail(settings):
    fixture_order = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
        router.post(f"{sw_base}/api/search/product").mock(side_effect=_sw_product_handler)
        router.post(f"{sw_base}/api/search/order").respond(200, json={"data": []})

        dhl_base = settings.dhl_base_url
        dhl_upload = router.post(
            f"{dhl_base}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")
        router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_SAMPLE_LABEL_XML
        )
        router.get(
            f"{dhl_base}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=_EMPTY_ACK_XML)

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
    fixture_order = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
        router.post(f"{sw_base}/api/search/product").mock(side_effect=_sw_product_handler)
        router.post(f"{sw_base}/api/search/order").respond(200, json={"data": []})

        dhl_base = settings.dhl_base_url
        router.post(f"{dhl_base}/transmission/{settings.dhl_username}").respond(
            200, text="<Ack/>"
        )
        router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_SAMPLE_LABEL_XML
        )
        router.get(
            f"{dhl_base}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=_EMPTY_ACK_XML)

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


def _akeneo_product(modell: str, color: str | None = None) -> dict:
    values: dict[str, list[dict]] = {
        "modell": [{"locale": None, "scope": None, "data": modell}]
    }
    if color is not None:
        values["color"] = [{"locale": None, "scope": None, "data": color}]
    return {"_embedded": {"items": [{"identifier": "400251", "values": values}]}}


def _mock_common_routes(
    router,
    settings,
    *,
    sw_knows_product: bool = True,
    b_ware: bool = False,
    sw_has_name: bool = True,
    variation_number: str | None = None,
) -> None:
    """Plenty + Shopware + DHL routes shared by the Akeneo pipeline tests.

    Only the synthetic clean order (900001, article 5050) is fetched. Shopware
    supplies the fallback name (``MN-5050 Schwarz``) unless ``sw_knows_product``
    is False, which is the "no model number anywhere" case. With ``b_ware`` the
    product carries the second-choice tag; ``sw_has_name`` False strips the
    fallback name off it, and ``variation_number`` sets the Plenty
    "Variantennummer" of the article.
    """
    plenty_base = settings.plenty.base_url
    router.post(f"{plenty_base}/rest/login").respond(200, json={"access_token": "tok"})
    router.get(f"{plenty_base}/rest/orders/shipping/countries").respond(
        200, json=[{"id": 1, "isoCode2": "DE"}]
    )
    router.get(f"{plenty_base}/rest/orders/search").respond(
        200,
        json={
            "isLastPage": True,
            "entries": [_to_jsonable(_synthetic_clean_order(variation_number))],
        },
    )
    router.post(f"{plenty_base}/rest/orders/900001/shipping/packages").respond(200, json={})

    sw_base = settings.shopware.base_url
    router.post(f"{sw_base}/api/oauth/token").respond(
        200, json={"access_token": "sw-tok", "expires_in": 600}
    )
    product = router.post(f"{sw_base}/api/search/product")
    if not sw_knows_product:
        product.respond(200, json={"data": []})
    elif b_ware:
        product.mock(
            side_effect=(
                _sw_b_ware_product_handler
                if sw_has_name
                else _sw_b_ware_unnamed_product_handler
            )
        )
    else:
        product.mock(side_effect=_sw_product_handler)
    router.post(f"{sw_base}/api/search/order").respond(200, json={"data": []})

    dhl_base = settings.dhl_base_url
    router.get(f"{dhl_base}/transmissionStatus/{settings.dhl_username}").respond(
        200, content=_SAMPLE_LABEL_XML
    )
    router.get(
        f"{dhl_base}/transmissionAcknowledgement/{settings.dhl_username}"
    ).respond(200, content=_EMPTY_ACK_XML)


async def test_akeneo_model_and_color_become_the_dhl_product_name(akeneo_settings):
    """The ProductName in the uploaded XML comes from the PIM, not from Plenty."""
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(router, settings)

        mk = settings.akeneomk.base_url
        router.post(f"{mk}/api/oauth/v1/token").respond(
            200, json={"access_token": "mk-tok", "expires_in": 3600}
        )
        router.get(f"{mk}/api/rest/v1/products").respond(
            200, json=_akeneo_product("UG 5005-30", color="kupfer_rose")
        )
        router.get(f"{mk}/api/rest/v1/attributes/color/options/kupfer_rose").respond(
            200, json={"labels": {"de_DE": "Kupfer Rose"}}
        )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>UG 5005-30 Kupfer Rose</ProductName>" in xml
    assert "Standalone-Artikel" not in xml  # the Plenty name is gone
    assert "MN-5050" not in xml  # nor the Shopware fallback


async def test_akeneo_outage_falls_back_to_shopware_and_still_uploads(akeneo_settings):
    """A PIM failure costs the better name, not the run."""
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(router, settings)

        for base in (settings.akeneomk.base_url, settings.akeneoml.base_url):
            router.post(f"{base}/api/oauth/v1/token").respond(500, text="pim down")

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>MN-5050 Schwarz</ProductName>" in xml


async def test_article_unknown_to_every_pim_keeps_the_shopware_name(akeneo_settings):
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(router, settings)

        for base in (settings.akeneomk.base_url, settings.akeneoml.base_url):
            router.post(f"{base}/api/oauth/v1/token").respond(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
            router.get(f"{base}/api/rest/v1/products").respond(
                200, json={"_embedded": {"items": []}}
            )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        await run_pipeline(settings)

    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>MN-5050 Schwarz</ProductName>" in xml


async def test_order_is_skipped_when_no_source_has_a_model_number(akeneo_settings):
    """Neither PIM nor Shopware knows the article → do not transmit it.

    The order must not reach DHL, must appear in the report mail with a reason,
    and must be named in the log.
    """
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client

        _mock_common_routes(router, settings, sw_knows_product=False)

        for base in (settings.akeneomk.base_url, settings.akeneoml.base_url):
            router.post(f"{base}/api/oauth/v1/token").respond(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
            router.get(f"{base}/api/rest/v1/products").respond(
                200, json={"_embedded": {"items": []}}
            )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    # Nothing transmitted.
    assert dhl_upload.call_count == 0
    assert summary.uploaded == 0
    assert summary.skipped == 1

    # Named in the log, with the stage and the reason.
    skip_events = [e for e in logs if e["event"] == "pipeline.order_skipped"]
    assert any(
        e["order_id"] == 900001
        and e["stage"] == "model_name"
        and "Modellnummer" in e["reason"]
        for e in skip_events
    )

    # And in the report mail.
    smtp_cls.assert_called_once()
    body = smtp_client.send_message.call_args[0][0].get_content()
    assert "900001" in body
    assert "Modellnummer" in body


async def test_akeneo_model_alone_satisfies_the_requirement(akeneo_settings):
    """Shopware knows nothing, but the PIM has a modell → order still ships."""
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(router, settings, sw_knows_product=False)

        mk = settings.akeneomk.base_url
        router.post(f"{mk}/api/oauth/v1/token").respond(
            200, json={"access_token": "tok", "expires_in": 3600}
        )
        router.get(f"{mk}/api/rest/v1/products").respond(
            200, json=_akeneo_product("K 7347 C")
        )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>K 7347 C</ProductName>" in xml


# ── Zweite Wahl: the Shopware "B-Ware" tag prefixes the ProductName ──────────


async def test_b_ware_article_takes_its_name_from_the_variation_number(akeneo_settings):
    """The Plenty "Variantennummer" beats the PIM for a second-choice article.

    A B-Ware variation has its own Plenty variation id, which the PIM does not
    carry — so even a PIM answer (here for the original article) must not win.
    """
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        _mock_common_routes(
            router, settings, b_ware=True, variation_number="UG 5005-30 Kupfer Rose"
        )

        mk = settings.akeneomk.base_url
        router.post(f"{mk}/api/oauth/v1/token").respond(
            200, json={"access_token": "mk-tok", "expires_in": 3600}
        )
        router.get(f"{mk}/api/rest/v1/products").respond(
            200, json=_akeneo_product("FALSCHES MODELL")
        )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>[ZW] UG 5005-30 Kupfer Rose</ProductName>" in xml
    assert "FALSCHES MODELL" not in xml
    assert any(
        e["event"] == "pipeline.second_choice_marked"
        and e["named_from_variation_number"] == 1
        for e in logs
    )


async def test_b_ware_variation_number_is_the_only_name_source_needed(akeneo_settings):
    """With neither a PIM nor a Shopware name, the variation number saves the order.

    Without it the article would have no model designation at all and
    ``require_model_names`` would skip the whole order.
    """
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(
            router,
            settings,
            b_ware=True,
            sw_has_name=False,
            variation_number="UG 5005-30 Kupfer Rose",
        )
        for base in (settings.akeneomk.base_url, settings.akeneoml.base_url):
            router.post(f"{base}/api/oauth/v1/token").respond(500, text="pim down")

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>[ZW] UG 5005-30 Kupfer Rose</ProductName>" in xml


async def test_b_ware_article_gets_the_zw_prefix_on_the_akeneo_name(akeneo_settings):
    """No variation number → the prefix sits in front of the PIM name."""
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        _mock_common_routes(router, settings, b_ware=True)

        mk = settings.akeneomk.base_url
        router.post(f"{mk}/api/oauth/v1/token").respond(
            200, json={"access_token": "mk-tok", "expires_in": 3600}
        )
        router.get(f"{mk}/api/rest/v1/products").respond(
            200, json=_akeneo_product("UG 5005-30", color="kupfer_rose")
        )
        router.get(f"{mk}/api/rest/v1/attributes/color/options/kupfer_rose").respond(
            200, json={"labels": {"de_DE": "Kupfer Rose"}}
        )

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>[ZW] UG 5005-30 Kupfer Rose</ProductName>" in xml
    assert any(e["event"] == "pipeline.second_choice_marked" for e in logs)


async def test_b_ware_article_gets_the_zw_prefix_on_the_shopware_name(akeneo_settings):
    """No variation number and no PIM answer → prefix on the fallback name."""
    settings = akeneo_settings
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
    ):
        _mock_common_routes(router, settings, b_ware=True)
        for base in (settings.akeneomk.base_url, settings.akeneoml.base_url):
            router.post(f"{base}/api/oauth/v1/token").respond(500, text="pim down")

        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        await run_pipeline(settings)

    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>[ZW] MN-5050 Schwarz</ProductName>" in xml


async def test_article_without_the_b_ware_tag_keeps_its_plain_name(settings):
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        _mock_common_routes(router, settings)
        dhl_upload = router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        await run_pipeline(settings)

    xml = dhl_upload.calls[0].request.content.decode("utf-8")
    assert "<ProductName>MN-5050 Schwarz</ProductName>" in xml
    assert "[ZW]" not in xml
    assert not any(e["event"] == "pipeline.second_choice_marked" for e in logs)


async def test_pipeline_without_akeneo_config_skips_the_pim_entirely(settings):
    """An install that leaves the Akeneo vars empty behaves exactly as before."""
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP"),
        patch("asyncio.sleep", new=_no_sleep),
        structlog.testing.capture_logs() as logs,
    ):
        _mock_common_routes(router, settings)
        router.post(
            f"{settings.dhl_base_url}/transmission/{settings.dhl_username}"
        ).respond(200, text="<Ack/>")

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1
    assert any(e["event"] == "pipeline.akeneo_disabled" for e in logs)


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


# ── DHL rejections (acknowledgement) ───────────────────────────────────────


async def test_rejected_upload_is_not_counted_and_lands_in_the_report(settings):
    """A rejected order was never accepted by DHL: it must not be reported as
    uploaded, and it must reach the report with DHL's own error code."""
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client
        _mock_common_routes(router, settings)
        router.post(f"{settings.dhl_base_url}/transmission/{settings.dhl_username}").respond(
            200, content=_rejection_ack(900001, "CUSTOMER_ALREADY_EXISTS")
        )
        # A rejected order never gets a label.
        router.get(
            f"{settings.dhl_base_url}/transmissionStatus/{settings.dhl_username}"
        ).respond(200, content=_EMPTY_STATUS_XML)

        summary = await run_pipeline(settings)

    assert summary.uploaded == 0
    assert summary.rejected == 1

    msg = smtp_client.send_message.call_args[0][0]
    assert "1 von DHL abgelehnt" in msg["Subject"]
    body = msg.get_content()
    assert "CUSTOMER_ALREADY_EXISTS" in body
    assert "900001" in body
    # Not also listed as "transmitted but no label" — it was never transmitted.
    assert LABEL_MISSING_REASON not in body
    assert summary.tracking_pushed == 0


async def test_acknowledgement_queue_errors_reach_the_report(settings):
    """Rejections for orders from earlier runs only exist in the queue."""
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client
        _mock_common_routes(router, settings)
        router.post(f"{settings.dhl_base_url}/transmission/{settings.dhl_username}").respond(
            200, content=_EMPTY_ACK_XML
        )
        router.get(
            f"{settings.dhl_base_url}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=_rejection_ack(241232, "ORDER_ALREADY_EXISTS"))

        summary = await run_pipeline(settings)

    assert summary.uploaded == 1  # this run's order went through
    msg = smtp_client.send_message.call_args[0][0]
    body = msg.get_content()
    assert "ORDER_ALREADY_EXISTS" in body
    assert "241232" in body


async def test_same_rejection_in_upload_and_queue_is_reported_once(settings):
    """An order uploaded with ack=true can also surface in the queue."""
    rejection = _rejection_ack(900001, "CUSTOMER_ALREADY_EXISTS")
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client
        _mock_common_routes(router, settings)
        router.post(f"{settings.dhl_base_url}/transmission/{settings.dhl_username}").respond(
            200, content=rejection
        )
        router.get(
            f"{settings.dhl_base_url}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=rejection)

        await run_pipeline(settings)

    body = smtp_client.send_message.call_args[0][0].get_content()
    assert body.count("CUSTOMER_ALREADY_EXISTS") == 1


async def test_acknowledgement_fetch_failure_does_not_lose_the_report(settings):
    """The labels are already written back when the queue is drained — a failure
    there must not cost the run its report mail."""
    with (
        respx.mock(assert_all_called=False) as router,
        patch("smtplib.SMTP") as smtp_cls,
        patch("asyncio.sleep", new=_no_sleep),
    ):
        smtp_client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_client
        _mock_common_routes(router, settings)
        router.post(f"{settings.dhl_base_url}/transmission/{settings.dhl_username}").respond(
            200, content=_rejection_ack(900001, "UNKNOWN_SERVICE")
        )
        router.get(
            f"{settings.dhl_base_url}/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(503)

        summary = await run_pipeline(settings)

    assert summary.rejected == 1
    body = smtp_client.send_message.call_args[0][0].get_content()
    assert "UNKNOWN_SERVICE" in body
