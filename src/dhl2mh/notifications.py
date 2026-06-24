"""Skipped-orders report mail. Plain-text, sent via SMTP STARTTLS."""

import smtplib
from datetime import datetime
from email.message import EmailMessage

import structlog

from dhl2mh.config import Settings
from dhl2mh.models import SkippedOrder

log = structlog.get_logger()


def send_skipped_orders_report(
    skipped: list[SkippedOrder],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """Email a German-language report of skipped orders to REPORT_RECIPIENT_EMAIL."""
    if not skipped:
        log.info("skipped_report.empty_nothing_to_send")
        return

    now = now or datetime.now()
    msg = EmailMessage()
    msg["From"] = f"{settings.smtp.from_name} <{settings.smtp.from_email}>"
    msg["To"] = settings.report_recipient_email
    msg["Subject"] = (
        f"DHL Workflow: {len(skipped)} Order(s) benötigen Prüfung — "
        f"{now.strftime('%d.%m.%Y %H:%M')}"
    )
    msg.set_content(_build_body(skipped, now))

    with smtplib.SMTP(settings.smtp.host, settings.smtp.port) as client:
        client.starttls()
        client.login(settings.smtp.username, settings.smtp.password)
        client.send_message(msg)

    log.info(
        "skipped_report.sent",
        count=len(skipped),
        recipient=settings.report_recipient_email,
    )


def _build_body(skipped: list[SkippedOrder], now: datetime) -> str:
    sep = "─" * 70
    lines = [
        "Hallo,",
        "",
        f"beim DHL-Workflow am {now.strftime('%d.%m.%Y')} um "
        f"{now.strftime('%H:%M')} Uhr benötigen {len(skipped)} Order(s) eine Prüfung "
        "(übersprungen oder ohne DHL-Label zurückgekommen).",
        "",
        "Diese Orders benötigen manuelle Überprüfung:",
        "",
        sep,
        "",
    ]
    for o in sorted(skipped, key=lambda s: s.order_id):
        lines.extend(
            [
                f"Order ID: {o.order_id}",
                f"  Datum:   {o.order_date.strftime('%d.%m.%Y %H:%M')}",
                f"  Kunde:   {o.customer_name}",
                f"  Artikel: {o.item_count}",
                f"  Grund:   {o.reason}",
                "",
            ]
        )
    lines.extend(
        [
            sep,
            "",
            "Bitte prüfen Sie diese Orders manuell in PlentyMarkets.",
            "",
            "Mit freundlichen Grüßen",
            "DHL Workflow Automation",
        ]
    )
    return "\n".join(lines)
