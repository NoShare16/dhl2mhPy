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


async def test_upload_posts_xml_with_correct_headers_and_path(settings):
    xml = b"<?xml version=\"1.0\"?><Transmission/>"
    with respx.mock(base_url=settings.dhl_base_url) as router:
        post = router.post(f"/transmission/{settings.dhl_username}").respond(
            200, text="<Ack/>"
        )
        async with DhlClient(settings) as client:
            body = await client.upload_order_xml(xml)

    assert body == "<Ack/>"
    req = post.calls[0].request
    assert req.content == xml
    assert req.headers["Authorization"] == _expected_basic_auth(
        settings.dhl_username, settings.dhl_password
    )
    assert req.headers["Content-Type"].startswith("text/xml")


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
