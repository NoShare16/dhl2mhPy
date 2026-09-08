"""Skipped-orders report mail. Plain-text, sent via SMTP STARTTLS."""

import smtplib
from datetime import datetime
from email.message import EmailMessage

import structlog

from dhl2mh.config import Settings
from dhl2mh.models import AckError, SkippedOrder

log = structlog.get_logger()


def send_skipped_orders_report(
    skipped: list[SkippedOrder],
    settings: Settings,
    *,
    ack_errors: list[AckError] | None = None,
    now: datetime | None = None,
) -> None:
    """Email a German-language report of skipped orders to REPORT_RECIPIENT_EMAIL.

    ``ack_errors`` are DHL's own rejections from the acknowledgement. They get
    their own section because they are a different kind of problem: the order
    passed every check here and DHL still refused it.
    """
    ack_errors = ack_errors or []
    if not skipped and not ack_errors:
        log.info("skipped_report.empty_nothing_to_send")
        return

    now = now or datetime.now()
    msg = EmailMessage()
    msg["From"] = f"{settings.smtp.from_name} <{settings.smtp.from_email}>"
    msg["To"] = settings.report_recipient_email
    msg["Subject"] = _build_subject(len(skipped), len(ack_errors), now)
    msg.set_content(_build_body(skipped, now, ack_errors))

    with smtplib.SMTP(settings.smtp.host, settings.smtp.port) as client:
        client.starttls()
        client.login(settings.smtp.username, settings.smtp.password)
        client.send_message(msg)

    log.info(
        "skipped_report.sent",
        count=len(skipped),
        ack_errors=len(ack_errors),
        recipient=settings.report_recipient_email,
    )


def _build_subject(skipped: int, ack_errors: int, now: datetime) -> str:
    parts = []
    if skipped:
        parts.append(f"{skipped} Order(s) benötigen Prüfung")
    if ack_errors:
        parts.append(f"{ack_errors} von DHL abgelehnt")
    return f"DHL Workflow: {' / '.join(parts)} — {now.strftime('%d.%m.%Y %H:%M')}"


def _build_body(
    skipped: list[SkippedOrder],
    now: datetime,
    ack_errors: list[AckError] | None = None,
) -> str:
    ack_errors = ack_errors or []
    sep = "─" * 70
    lines = [
        "Hallo,",
        "",
        f"beim DHL-Workflow am {now.strftime('%d.%m.%Y')} um "
        f"{now.strftime('%H:%M')} Uhr sind {_summary(len(skipped), len(ack_errors))}.",
        "",
    ]

    if skipped:
        lines.extend(
            [
                "Diese Orders benötigen manuelle Überprüfung:",
                "",
                sep,
                "",
            ]
        )
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
        lines.extend([sep, ""])

    if ack_errors:
        lines.extend(
            [
                "Von DHL abgelehnt (Rückmeldung des Imports):",
                "",
                sep,
                "",
            ]
        )
        for err in sorted(ack_errors, key=lambda e: (e.order_id or 0)):
            lines.extend(
                [
                    f"Order ID: {err.order_id if err.order_id is not None else 'unbekannt'}",
                    f"  Code:    {err.error_code}",
                    f"  Meldung: {err.error_text or '—'}",
                    "",
                ]
            )
        lines.extend(
            [
                sep,
                "",
                "Abgelehnte Aufträge sind bei DHL nicht angekommen und erzeugen kein "
                "Label. Sie tauchen im Statusabruf nicht auf.",
                "",
            ]
        )

    lines.extend(
        [
            "Bitte prüfen Sie diese Orders manuell in PlentyMarkets.",
            "",
            "Mit freundlichen Grüßen",
            "DHL Workflow Automation",
        ]
    )
    return "\n".join(lines)


def _summary(skipped: int, ack_errors: int) -> str:
    parts = []
    if skipped:
        parts.append(
            f"{skipped} Order(s) zu prüfen (übersprungen oder ohne DHL-Label "
            "zurückgekommen)"
        )
    if ack_errors:
        parts.append(f"{ack_errors} Order(s) von DHL abgelehnt worden")
    return " und ".join(parts)
