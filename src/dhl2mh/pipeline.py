"""End-to-end workflow.

Plenty orders → filter → enrich (Shopware categories) → resolve services
→ build DHL XML → upload → wait → pull labels → push OrderIdent back to Plenty
→ mail report of skipped orders.

One pass per cron invocation: ``python -m dhl2mh`` or the ``dhl2mh`` CLI.
"""

import asyncio
from typing import NamedTuple

import structlog

from dhl2mh.clients.dhl import DhlClient
from dhl2mh.clients.plenty import PlentyClient
from dhl2mh.clients.shopware import ShopwareClient
from dhl2mh.config import Settings, get_settings
from dhl2mh.filter import filter_orders
from dhl2mh.mapper import map_order
from dhl2mh.mapping import STOCK_LIMITATION_ARTICLE
from dhl2mh.models import PackageData, PlentyOrder, SkippedOrder
from dhl2mh.notifications import send_skipped_orders_report
from dhl2mh.service_resolver import resolve_orders
from dhl2mh.xml_builder import OrderXmlBuilder

log = structlog.get_logger()


class PipelineSummary(NamedTuple):
    fetched: int
    uploaded: int
    labels_received: int
    tracking_pushed: int
    skipped: int


async def run_pipeline(
    settings: Settings | None = None,
    *,
    items_per_page: int = 50,
    category_concurrency: int = 5,
) -> PipelineSummary:
    settings = settings or get_settings()

    async with (
        PlentyClient(settings) as plenty,
        ShopwareClient(settings) as shopware,
        DhlClient(settings) as dhl,
    ):
        # 1. Fetch
        countries = await plenty.get_countries()
        api_orders = [o async for o in plenty.iter_orders(items_per_page=items_per_page)]
        log.info("pipeline.fetched", count=len(api_orders))

        # 2. Map to domain
        orders = [map_order(api, countries) for api in api_orders]

        # 3. Filter
        filtered = filter_orders(orders)
        log.info(
            "pipeline.filtered",
            passed=len(filtered.passed),
            skipped=len(filtered.skipped),
        )

        if not filtered.passed:
            _maybe_send_report(filtered.skipped, settings)
            return PipelineSummary(
                fetched=len(api_orders),
                uploaded=0,
                labels_received=0,
                tracking_pushed=0,
                skipped=len(filtered.skipped),
            )

        # 4. Shopware categories (parallel)
        await _enrich_categories(filtered.passed, shopware, concurrency=category_concurrency)

        # 5. Resolve services (MatchCodes, SWG auto-add, VPR auto-add)
        resolved = resolve_orders(filtered.passed)
        log.info(
            "pipeline.resolved",
            passed=len(resolved.passed),
            skipped=len(resolved.skipped),
        )

        # 6. Build XML + upload
        builder = OrderXmlBuilder(
            sending_party_id=settings.dhl_username,
            sender_partner_id="3" if settings.is_production else "1",
        )
        uploaded = 0
        for order in resolved.passed:
            xml = builder.build(order)
            await dhl.upload_order_xml(xml)
            uploaded += 1
        log.info("pipeline.uploaded", count=uploaded)

        # 7. Wait for DHL processing, then pull labels
        wait_s = settings.dhl.label_wait_seconds
        if wait_s > 0 and uploaded > 0:
            log.info("pipeline.waiting_for_labels", seconds=wait_s)
            await asyncio.sleep(wait_s)

        labels = await dhl.get_labels()
        log.info("pipeline.labels_received", count=len(labels))

        # 8. Push OrderIdent back to Plenty
        tracking_pushed = 0
        for label in labels:
            try:
                await plenty.update_package(
                    label.order_id,
                    PackageData(package_number=label.order_ident),
                )
                tracking_pushed += 1
            except RuntimeError as e:
                # one failing push must not abort the rest
                log.warning(
                    "pipeline.tracking_push_failed",
                    order_id=label.order_id,
                    error=str(e),
                )

        # 9. Mail report
        _maybe_send_report(filtered.skipped + resolved.skipped, settings)

        return PipelineSummary(
            fetched=len(api_orders),
            uploaded=uploaded,
            labels_received=len(labels),
            tracking_pushed=tracking_pushed,
            skipped=len(filtered.skipped) + len(resolved.skipped),
        )


# ── helpers ────────────────────────────────────────────────────────────────


async def _enrich_categories(
    orders: list[PlentyOrder],
    shopware: ShopwareClient,
    *,
    concurrency: int,
) -> None:
    article_ids = {
        item.id
        for o in orders
        for item in o.order_items
        if item.stock_limitation in STOCK_LIMITATION_ARTICLE
    }
    if not article_ids:
        return
    log.info("pipeline.enriching_categories", articles=len(article_ids))
    cats = await shopware.get_categories_bulk(article_ids, concurrency=concurrency)
    for o in orders:
        for item in o.order_items:
            if item.stock_limitation in STOCK_LIMITATION_ARTICLE:
                item.categories = cats.get(str(item.id), [])


def _maybe_send_report(skipped: list[SkippedOrder], settings: Settings) -> None:
    if not skipped:
        return
    try:
        send_skipped_orders_report(skipped, settings)
    except Exception as e:
        # mail failure must not crash the pipeline
        log.error("pipeline.skipped_report_failed", error=str(e))
