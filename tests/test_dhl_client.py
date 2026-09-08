import base64
import hashlib

import pytest
import respx

from dhl2mh.clients.dhl import DhlClient


def _expected_basic_auth(user: str, password: str) -> str:
    sha1_hex = hashlib.sha1(password.encode()).hexdigest().upper()
    return "Basic " + base64.b64encode(f"{user}:{sha1_hex}".encode()).decode()


def test_basic_auth_is_user_colon_sha1_upper_b64(settings):
    """C# contract: Basic base64('USER:SHA1_UPPER_HEX(PW)'). Wrong format → 401."""
    client = DhlClient(settings)
    assert client._auth_header == _expected_basic_auth(
        settings.dhl_username, settings.dhl_password
    )


# ── acknowledgement samples (shape taken from real DHL UAT answers) ────────

ACCEPTED_ACK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ns6:TransmissionAcknowledgement xmlns:ns6="http://www.it4logistics.de/i4ldata/ext">
  <SendingPartyID>DELIVERIT</SendingPartyID>
  <ReceivingPartyID>HDE</ReceivingPartyID>
  <AcknowledgementDetails>
    <Message>
      <MessageContent>
        <ns6:Order>
          <OrderId><System>HDE</System><Id>908131715</Id></OrderId>
          <OrderNr>908131715</OrderNr>
        </ns6:Order>
      </MessageContent>
    </Message>
  </AcknowledgementDetails>
</ns6:TransmissionAcknowledgement>
"""

REJECTED_ACK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ns6:TransmissionAcknowledgement xmlns:ns6="http://www.it4logistics.de/i4ldata/ext">
  <SendingPartyID>DELIVERIT</SendingPartyID>
  <ReceivingPartyID>HDE</ReceivingPartyID>
  <AcknowledgementDetails>
    <ErrorResponse>Customer [HDE, 4099999] already exists!</ErrorResponse>
    <ErrorCode>CUSTOMER_ALREADY_EXISTS</ErrorCode>
    <Message>
      <MessageContent>
        <ns6:Order>
          <OrderId><System>HDE</System><Id>908133604</Id></OrderId>
          <OrderNr>908133604</OrderNr>
        </ns6:Order>
      </MessageContent>
    </Message>
  </AcknowledgementDetails>
</ns6:TransmissionAcknowledgement>
"""


async def test_upload_posts_xml_with_correct_headers_and_path(settings):
    xml = b"<?xml version=\"1.0\"?><Transmission/>"
    with respx.mock(base_url=settings.dhl_base_url) as router:
        post = router.post(f"/transmission/{settings.dhl_username}").respond(
            200, content=ACCEPTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            errors = await client.upload_order_xml(xml)

    assert errors == []  # no ErrorCode in the answer → accepted
    req = post.calls[0].request
    assert req.content == xml
    assert req.headers["Authorization"] == _expected_basic_auth(
        settings.dhl_username, settings.dhl_password
    )
    assert req.headers["Content-Type"].startswith("text/xml")


async def test_upload_requests_the_acknowledgement(settings):
    """Without ``ack=true`` the body comes back empty and a rejection is
    invisible until someone drains the (consume-once) queue."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        post = router.post(f"/transmission/{settings.dhl_username}").respond(
            200, content=ACCEPTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            await client.upload_order_xml(b"<x/>")

    assert post.calls[0].request.url.params["ack"] == "true"


async def test_upload_returns_rejection_from_acknowledgement(settings):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.post(f"/transmission/{settings.dhl_username}").respond(
            200, content=REJECTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            errors = await client.upload_order_xml(b"<x/>", order_id=908133604)

    assert len(errors) == 1
    assert errors[0].error_code == "CUSTOMER_ALREADY_EXISTS"
    assert errors[0].error_text == "Customer [HDE, 4099999] already exists!"
    assert errors[0].order_id == 908133604


async def test_rejected_upload_is_http_200_not_an_exception(settings):
    """The whole point: DHL accepts the transmission and refuses the order."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.post(f"/transmission/{settings.dhl_username}").respond(
            200, content=REJECTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            errors = await client.upload_order_xml(b"<x/>")

    assert errors  # reported as data, not raised


async def test_upload_failure_raises(settings):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.post(f"/transmission/{settings.dhl_username}").respond(400, text="bad xml")
        async with DhlClient(settings) as client:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await client.upload_order_xml(b"<x/>")


# ── label parsing ──────────────────────────────────────────────────────────

SAMPLE_LABEL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<dsi:Transmission xmlns:dsi="http://www.it4logistics.de/i4ldata/ext">
  <Messages>
    <MessageContent>
      <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="ns6:OrderDocument">
        <OrderId><System>HDE</System><Id>12345</Id></OrderId>
        <OrderNr>12345</OrderNr>
        <OrderIdent>00340434161094018448</OrderIdent>
        <Document xsi:type="ns6:Label">
          <Barcode>00340434161094018448</Barcode>
          <Content>UERGSEVSRQ==</Content>
          <Stamp>2025-01-15T12:34:56</Stamp>
        </Document>
      </ns6:Status>
    </MessageContent>
  </Messages>
  <Messages>
    <MessageContent>
      <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="ns6:OrderDocument">
        <OrderId><System>HDE</System><Id>67890</Id></OrderId>
        <OrderIdent>00340434161094999999</OrderIdent>
        <Document xsi:type="ns6:Label">
          <Barcode>00340434161094999999</Barcode>
        </Document>
      </ns6:Status>
    </MessageContent>
  </Messages>
  <Messages>
    <MessageContent>
      <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="ns6:OrderConfirmation">
        <OrderId><System>HDE</System><Id>99999</Id></OrderId>
      </ns6:Status>
    </MessageContent>
  </Messages>
  <Messages>
    <MessageContent>
      <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="ns6:OrderDocument">
        <OrderId><System>HDE</System><Id>55555</Id></OrderId>
        <OrderIdent>otherident</OrderIdent>
        <Document xsi:type="ns6:Invoice">
          <Content>...</Content>
        </Document>
      </ns6:Status>
    </MessageContent>
  </Messages>
</dsi:Transmission>
"""


def test_parse_label_xml_extracts_only_label_documents(settings):
    labels = DhlClient._parse_label_xml(SAMPLE_LABEL_XML)
    assert [l.order_id for l in labels] == [12345, 67890]
    assert labels[0].order_ident == "00340434161094018448"
    assert labels[0].barcode == "00340434161094018448"
    assert labels[1].order_ident == "00340434161094999999"


def test_parse_label_xml_empty_returns_empty():
    xml = b'<?xml version="1.0"?><Transmission/>'
    assert DhlClient._parse_label_xml(xml) == []


def test_parse_label_xml_skips_when_orderident_missing():
    xml = b"""<?xml version="1.0"?>
    <Transmission xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <Messages><MessageContent>
        <ns6:Status xsi:type="ns6:OrderDocument">
          <OrderId><Id>1</Id></OrderId>
          <Document xsi:type="ns6:Label"><Barcode>X</Barcode></Document>
        </ns6:Status>
      </MessageContent></Messages>
    </Transmission>"""
    assert DhlClient._parse_label_xml(xml) == []


async def test_get_labels_hits_transmissionstatus_endpoint(settings):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        get = router.get(f"/transmissionStatus/{settings.dhl_username}").respond(
            200, content=SAMPLE_LABEL_XML
        )
        async with DhlClient(settings) as client:
            labels = await client.get_labels()

    assert len(labels) == 2
    assert get.calls[0].request.headers["Authorization"] == _expected_basic_auth(
        settings.dhl_username, settings.dhl_password
    )


_DUP_ORDER_LABEL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<dsi:Transmission xmlns:dsi="http://www.it4logistics.de/i4ldata/ext">
  <Messages><MessageContent>
    <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="ns6:OrderDocument">
      <OrderId><System>HDE</System><Id>237601</Id></OrderId>
      <OrderIdent>680214548424</OrderIdent>
      <Document xsi:type="ns6:Label"><Barcode>680214548424</Barcode></Document>
    </ns6:Status>
  </MessageContent></Messages>
  <Messages><MessageContent>
    <ns6:Status xmlns:ns6="http://www.it4logistics.de/i4ldata/ext"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="ns6:OrderDocument">
      <OrderId><System>HDE</System><Id>237601</Id></OrderId>
      <OrderIdent>680214548424</OrderIdent>
      <Document xsi:type="ns6:Label"><Barcode>680214548424</Barcode></Document>
    </ns6:Status>
  </MessageContent></Messages>
</dsi:Transmission>
"""


async def test_get_labels_dedupes_repeated_order(settings):
    """No multi-package shipments: one ident per order even if the status repeats."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionStatus/{settings.dhl_username}").respond(
            200, content=_DUP_ORDER_LABEL_XML
        )
        async with DhlClient(settings) as client:
            labels = await client.get_labels()

    assert len(labels) == 1
    assert labels[0].order_id == 237601
    assert labels[0].order_ident == "680214548424"


async def test_get_labels_failure_raises(settings):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionStatus/{settings.dhl_username}").respond(503)
        async with DhlClient(settings) as client:
            with pytest.raises(RuntimeError, match="HTTP 503"):
                await client.get_labels()


def test_prod_env_targets_prod_url_and_credentials(monkeypatch, settings):
    """Sanity: switching APP_ENV swaps URL + password the client uses."""
    monkeypatch.setenv("APP_ENV", "prod")
    from dhl2mh.config import Settings

    prod_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    client = DhlClient(prod_settings)
    assert "prod" in prod_settings.dhl_base_url
    assert client._auth_header == _expected_basic_auth(
        prod_settings.dhl_username, prod_settings.dhl_password
    )


# ── acknowledgement queue ──────────────────────────────────────────────────


async def test_acknowledgement_is_archived_before_parsing(settings, tmp_path):
    """Consume-once: the raw answer must survive on disk even if parsing fails,
    because there is no second chance to fetch it."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionAcknowledgement/{settings.dhl_username}").respond(
            200, content=REJECTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            path, errors = await client.fetch_acknowledgements(tmp_path / "acks")

    assert path.exists()
    assert path.read_bytes() == REJECTED_ACK_XML
    assert [(e.order_id, e.error_code) for e in errors] == [
        (908133604, "CUSTOMER_ALREADY_EXISTS")
    ]


async def test_acknowledgement_uses_the_long_read_timeout(settings, tmp_path):
    """The 60 s default is not enough — a timeout drains the queue anyway."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        route = router.get(
            f"/transmissionAcknowledgement/{settings.dhl_username}"
        ).respond(200, content=ACCEPTED_ACK_XML)
        async with DhlClient(settings) as client:
            await client.fetch_acknowledgements(tmp_path / "acks")

    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == settings.dhl.ack_read_timeout_seconds
    assert timeout["read"] > 60.0


async def test_acknowledgement_without_errors_reports_none(settings, tmp_path):
    """AcknowledgementDetails is always present — it echoes the order back.
    Only an ErrorCode marks a rejection."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionAcknowledgement/{settings.dhl_username}").respond(
            200, content=ACCEPTED_ACK_XML
        )
        async with DhlClient(settings) as client:
            _, errors = await client.fetch_acknowledgements(tmp_path / "acks")

    assert errors == []


async def test_empty_acknowledgement_queue_is_not_an_error(settings, tmp_path):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionAcknowledgement/{settings.dhl_username}").respond(
            200, content=b""
        )
        async with DhlClient(settings) as client:
            path, errors = await client.fetch_acknowledgements(tmp_path / "acks")

    assert errors == []
    assert path.exists()


async def test_acknowledgement_failure_raises(settings, tmp_path):
    with respx.mock(base_url=settings.dhl_base_url) as router:
        router.get(f"/transmissionAcknowledgement/{settings.dhl_username}").respond(500)
        async with DhlClient(settings) as client:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await client.fetch_acknowledgements(tmp_path / "acks")


def test_unparseable_acknowledgement_yields_no_errors():
    """A garbled answer must not be mistaken for 'everything fine' silently —
    it is logged — but it must not crash the run either."""
    assert DhlClient._parse_ack_errors(b"<not xml") == []
    assert DhlClient._parse_ack_errors(b"") == []


# ── targeted status pull ───────────────────────────────────────────────────


async def test_get_labels_for_order_filters_by_system_and_id(settings):
    """Per-order pull does not drain the collective queue."""
    with respx.mock(base_url=settings.dhl_base_url) as router:
        route = router.get(f"/transmissionStatus/{settings.dhl_username}").respond(
            200, content=SAMPLE_LABEL_XML
        )
        async with DhlClient(settings) as client:
            labels = await client.get_labels_for_order(12345)

    # The filtering itself is DHL's: what matters here is that the query goes
    # out in the documented {System}_{Id} shape and the answer is parsed.
    assert route.calls[0].request.url.params["orderId"] == f"{settings.dhl_username}_12345"
    assert 12345 in [label.order_id for label in labels]
