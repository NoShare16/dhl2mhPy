"""Command-line entry point: ``dhl2mh run`` for a one-shot workflow execution."""

import asyncio
from typing import Annotated

import typer

from dhl2mh.logging_setup import setup_logging
from dhl2mh.pipeline import run_pipeline

app = typer.Typer(
    help="DHL DeliverIT pipeline: Plenty → DHL upload → tracking back to Plenty.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def run(
    items_per_page: Annotated[
        int, typer.Option(help="Plenty pagination size.")
    ] = 50,
    concurrency: Annotated[
        int, typer.Option(help="Parallel Shopware category fetches.")
    ] = 5,
    log_level: Annotated[
        str, typer.Option(help="DEBUG / INFO / WARNING / ERROR.")
    ] = "INFO",
) -> None:
    """Run the full DHL workflow once. Designed for cron."""
    setup_logging(level=log_level)
    summary = asyncio.run(
        run_pipeline(items_per_page=items_per_page, category_concurrency=concurrency)
    )
    typer.echo(
        f"fetched={summary.fetched} "
        f"uploaded={summary.uploaded} "
        f"labels={summary.labels_received} "
        f"pushed={summary.tracking_pushed} "
        f"skipped={summary.skipped}"
    )
