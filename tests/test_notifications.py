from datetime import datetime
from unittest.mock import MagicMock, patch

from dhl2mh.models import AckError, SkippedOrder
from dhl2mh.notifications import send_skipped_orders_report


def _skipped(**overrides) -> SkippedOrder:
    base = dict(
        order_id=42,
        order_date=datetime(2025, 1, 15, 10, 30),
        reason="PackageNumber vorhanden: X",
        customer_name="Max Mustermann",
        item_count=3,
    )
    base.update(overrides)
    return SkippedOrder(**base)


def test_empty_list_does_not_open_smtp_connection(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        send_skipped_orders_report([], settings)
    smtp_cls.assert_not_called()


def test_report_connects_starts_tls_authenticates_sends(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client

        send_skipped_orders_report([_skipped()], settings)

    smtp_cls.assert_called_once_with(settings.smtp.host, settings.smtp.port)
    client.starttls.assert_called_once()
    client.login.assert_called_once_with(settings.smtp.username, settings.smtp.password)
    client.send_message.assert_called_once()


def test_message_headers_and_body_contain_key_data(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client

        send_skipped_orders_report(
            [_skipped(order_id=99, reason="Artikel ohne Gewichtsangabe: 12345")],
            settings,
            now=datetime(2025, 3, 1, 14, 5),
        )

    msg = client.send_message.call_args[0][0]
    assert msg["To"] == settings.report_recipient_email
    assert settings.smtp.from_email in msg["From"]
    assert "1 Order(s) benötigen Prüfung" in msg["Subject"]
    assert "01.03.2025 14:05" in msg["Subject"]

    body = msg.get_content()
    assert "Order ID: 99" in body
    assert "Max Mustermann" in body
    assert "Artikel ohne Gewichtsangabe: 12345" in body


def test_multiple_skipped_orders_are_sorted_by_id(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client

        send_skipped_orders_report(
            [_skipped(order_id=300), _skipped(order_id=100), _skipped(order_id=200)],
            settings,
        )

    body = client.send_message.call_args[0][0].get_content()
    pos_100 = body.find("Order ID: 100")
    pos_200 = body.find("Order ID: 200")
    pos_300 = body.find("Order ID: 300")
    assert 0 < pos_100 < pos_200 < pos_300


# ── DHL rejections ─────────────────────────────────────────────────────────


def _ack(order_id, code="CUSTOMER_ALREADY_EXISTS", text="Customer [HDE, 4099999] already exists!"):
    return AckError(order_id=order_id, error_code=code, error_text=text)


def test_ack_errors_alone_still_send_a_report(settings):
    """No skipped orders, but DHL refused one — that has to reach someone."""
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        send_skipped_orders_report([], settings, ack_errors=[_ack(241232)])

    msg = client.send_message.call_args[0][0]
    assert "1 von DHL abgelehnt" in msg["Subject"]
    body = msg.get_content()
    assert "241232" in body
    assert "CUSTOMER_ALREADY_EXISTS" in body
    assert "Customer [HDE, 4099999] already exists!" in body


def test_nothing_at_all_sends_no_mail(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        send_skipped_orders_report([], settings, ack_errors=[])
    smtp_cls.assert_not_called()


def test_skipped_and_rejected_are_separate_sections(settings):
    skipped = SkippedOrder(
        order_id=900002,
        order_date=datetime(2026, 9, 8, 9, 0),
        reason="Kein former_parent",
        customer_name="Max Mustermann",
        item_count=1,
    )
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        send_skipped_orders_report([skipped], settings, ack_errors=[_ack(241232)])

    msg = client.send_message.call_args[0][0]
    assert "1 Order(s) benötigen Prüfung" in msg["Subject"]
    assert "1 von DHL abgelehnt" in msg["Subject"]
    body = msg.get_content()
    assert "Diese Orders benötigen manuelle Überprüfung:" in body
    assert "Von DHL abgelehnt" in body
    # Each order appears under its own heading, not mixed into the other list.
    assert body.index("900002") < body.index("Von DHL abgelehnt") < body.index("241232")


def test_rejection_without_order_id_is_still_reported(settings):
    with patch("smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        send_skipped_orders_report([], settings, ack_errors=[_ack(None, "UNKNOWN_PARTNER", "")])

    body = client.send_message.call_args[0][0].get_content()
    assert "unbekannt" in body
    assert "UNKNOWN_PARTNER" in body
