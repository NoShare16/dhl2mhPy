from datetime import datetime
from unittest.mock import MagicMock, patch

from dhl2mh.models import SkippedOrder
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
    assert "1 Order(s) übersprungen" in msg["Subject"]
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
