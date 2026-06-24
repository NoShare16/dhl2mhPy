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
from dhl2mh.shopware_mapping import (
    assign_former_parent_ids,
    assign_water_connection,
    require_service_former_parent_ids,
)
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
    dry_run: bool = False,
) -> PipelineSummary:
    settings = settings or get_settings()
    if dry_run:
        log.info("pipeline.dry_run_enabled")

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

        # 3. Shopware order enrichment (parallel): former-parent ids + the
        # Festwasser flag. Runs before the filter so the bundle grouping (keyed on
        # former_parent_id) is final when the filter validates the bundle
        # structure and the resolver folds services into their article.
        await _enrich_from_shopware_order(orders, shopware, concurrency=category_concurrency)

        # 4. Skip orders whose service positions still lack a former_parent_id.
        fp = require_service_former_parent_ids(orders)
        log.info(
            "pipeline.former_parent",
            passed=len(fp.passed),
            skipped=len(fp.skipped),
        )
        _log_skipped("former_parent", fp.skipped)

        # 5. Filter (bundle structure, package number, article weight, …)
        filtered = filter_orders(fp.passed)
        log.info(
            "pipeline.filtered",
            passed=len(filtered.passed),
            skipped=len(filtered.skipped),
        )
        _log_skipped("filter", filtered.skipped)

        if not filtered.passed:
            _maybe_send_report(fp.skipped + filtered.skipped, settings, dry_run=dry_run)
            return PipelineSummary(
                fetched=len(api_orders),
                uploaded=0,
                labels_received=0,
                tracking_pushed=0,
                skipped=len(fp.skipped) + len(filtered.skipped),
            )

        # 6. Shopware categories (parallel)
        await _enrich_categories(filtered.passed, shopware, concurrency=category_concurrency)

        # 7. Resolve services (MatchCodes, SWG auto-add, VPR auto-add)
        resolved = resolve_orders(filtered.passed)
        log.info(
            "pipeline.resolved",
            passed=len(resolved.passed),
            skipped=len(resolved.skipped),
        )
        _log_skipped("resolve", resolved.skipped)

        # 8. Build XML + upload
        builder = OrderXmlBuilder(
            sending_party_id=settings.dhl_username,
            sender_partner_id="3" if settings.is_production else "1",
        )
        uploaded_ids: list[int] = []
        for order in resolved.passed:
            xml = builder.build(order)
            await dhl.upload_order_xml(xml, order_id=order.id)
            uploaded_ids.append(order.id)
        uploaded = len(uploaded_ids)
        log.info("pipeline.uploaded", count=uploaded, order_ids=uploaded_ids)

        # 9. Wait for DHL processing, then pull labels
        wait_s = settings.dhl.label_wait_seconds
        if wait_s > 0 and uploaded > 0:
            log.info("pipeline.waiting_for_labels", seconds=wait_s)
            await asyncio.sleep(wait_s)

        labels = await dhl.get_labels()
        log.info("pipeline.labels_received", count=len(labels))

        # Reconcile: an order transmitted this run that produced no label is the
        # silent-failure case (DHL rejected it, or its label is slow). The single
        # status pull is a snapshot, so this is a warning to investigate, not an
        # error — a later run may still pick the label up.
        label_ids = {label.order_id for label in labels}
        missing_labels = [oid for oid in uploaded_ids if oid not in label_ids]
        if missing_labels:
            log.warning(
                "pipeline.labels_missing",
                count=len(missing_labels),
                order_ids=missing_labels,
            )

        # 10. Push OrderIdent back to Plenty (skipped in dry-run)
        tracking_pushed = 0
        for label in labels:
            if dry_run:
                log.info(
                    "pipeline.dry_run_skip_tracking_push",
                    order_id=label.order_id,
                    order_ident=label.order_ident,
                )
                continue
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

        # 11. Mail report (skipped in dry-run)
        _maybe_send_report(
            filtered.skipped + fp.skipped + resolved.skipped, settings, dry_run=dry_run
        )

        return PipelineSummary(
            fetched=len(api_orders),
            uploaded=uploaded,
            labels_received=len(labels),
            tracking_pushed=tracking_pushed,
            skipped=len(filtered.skipped) + len(fp.skipped) + len(resolved.skipped),
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _log_skipped(stage: str, skipped: list[SkippedOrder]) -> None:
    """One log line per skipped order so the cron log shows *which* order was
    dropped *why* — the reason otherwise lives only in the report mail."""
    for order in skipped:
        log.info(
            "pipeline.order_skipped",
            stage=stage,
            order_id=order.order_id,
            reason=order.reason,
        )


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


async def _enrich_from_shopware_order(
    orders: list[PlentyOrder],
    shopware: ShopwareClient,
    *,
    concurrency: int,
) -> None:
    """Enrich orders from their Shopware order: former_parent_id + Festwasser.

    Only orders carrying a shopware_id (the Shopware orderNumber) are queried;
    manually created orders without one keep their Plenty-seeded values. The
    Shopware former_parent_id wins when present, but an empty value never clears
    a filled field (handled in ``assign_former_parent_ids``).
    """
    targets = [o for o in orders if o.shopware_id]
    if not targets:
        return
    log.info("pipeline.enriching_from_shopware_order", orders=len(targets))
    sem = asyncio.Semaphore(concurrency)

    async def enrich_one(order: PlentyOrder) -> None:
        async with sem:
            sw_order = await shopware.get_order(order.shopware_id)  # type: ignore[arg-type]
        if sw_order is None:
            log.warning("pipeline.shopware_order_not_found", shopware_id=order.shopware_id)
            return
        matched = assign_former_parent_ids(order, sw_order)
        water = assign_water_connection(order, sw_order)
        log.info(
            "pipeline.shopware_order_matched",
            order_id=order.id,
            shopware_id=order.shopware_id,
            former_parent_matched=matched,
            water_connection_matched=water,
        )

    await asyncio.gather(*(enrich_one(o) for o in targets))


def _maybe_send_report(
    skipped: list[SkippedOrder], settings: Settings, *, dry_run: bool = False
) -> None:
    if not skipped:
        return
    if dry_run:
        log.info("pipeline.dry_run_skip_report", would_report=len(skipped))
        return
    try:
        send_skipped_orders_report(skipped, settings)
    except Exception as e:
        # mail failure must not crash the pipeline
        log.error("pipeline.skipped_report_failed", error=str(e))
