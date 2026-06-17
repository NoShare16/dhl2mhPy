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


@app.callback()
def _main() -> None:
    """Keep Typer in multi-command mode so ``run`` stays a named subcommand.

    Without a callback, a single-command Typer app collapses the command and
    rejects the ``run`` argument (the README documents ``dhl2mh run``).
    """


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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run fully (incl. DHL UAT upload + label pull) but do NOT write "
            "tracking back to Plenty and do NOT send the report mail.",
        ),
    ] = False,
) -> None:
    """Run the full DHL workflow once. Designed for cron."""
    setup_logging(level=log_level)
    summary = asyncio.run(
        run_pipeline(
            items_per_page=items_per_page,
            category_concurrency=concurrency,
            dry_run=dry_run,
        )
    )
    if dry_run:
        typer.echo("DRY RUN — no Plenty tracking write-back, no report mail sent.")
    typer.echo(
        f"fetched={summary.fetched} "
        f"uploaded={summary.uploaded} "
        f"labels={summary.labels_received} "
        f"pushed={summary.tracking_pushed} "
        f"skipped={summary.skipped}"
    )
