import asyncio
import logging
import time
from typing import Any, List

import click
import typer
from azure.identity.aio import AzureCliCredential
from rich import print

from .aad import query_aad
from .context import CollectorContext
from .enums import Cloud, EnumMode
from .utils import gen_results_tables

app = typer.Typer(
    name="Stormspotter Collector CLI",
    context_settings={"help_option_names": ["-h", "--help"]},
    add_completion=False,
)

log = logging.getLogger("rich")


async def start_collect(ctx: CollectorContext):

    # Create the directory for output
    ctx.output_dir.mkdir(parents=True)

    # Create and run tasks for AAD and/or ARM
    tasks = []

    if EnumMode.AAD in ctx.mode:
        tasks.append(asyncio.create_task(query_aad(ctx)))

    if EnumMode.ARM in ctx.mode:
        pass

    await asyncio.wait(tasks)

    # Ensure credential object gets closed properly
    await ctx.cred.close()

    # Print results
    print("\n", gen_results_tables(ctx._aad_results, ctx._arm_results))


@click.pass_context
def _begin_run(ctx: typer.Context, result: Any):
    """Invoke async run of collector"""

    collector_ctx: CollectorContext = ctx.obj["ctx"]
    if collector_ctx.cred:
        start_time = time.time()
        asyncio.run(start_collect(collector_ctx))
        log.info(f"--- COMPLETION TIME: {time.time() - start_time} seconds")


@app.callback(result_callback=_begin_run)
def main(ctx: typer.Context):
    """
    Stormspotter Collector CLI.
    """
    ctx.ensure_object(dict)


@app.command()
def azcli(
    ctx: typer.Context,
    cloud: Cloud = typer.Option(
        Cloud.PUBLIC, "--cloud", help="Cloud environment", metavar=""
    ),
    mode: EnumMode = typer.Option(
        EnumMode.BOTH, "--mode", help="AAD, ARM, or both", metavar=""
    ),
    backfill: bool = typer.Option(
        False,
        "--backfill",
        help="Perform AAD enumeration only for ARM RBAC object IDs",
        metavar="",
    ),
    include_subs: List[str] = typer.Option(
        [], "--include-subs", "-i", help="Only scan specific subscriptions", metavar=""
    ),
    exclude_subs: List[str] = typer.Option(
        [], "--exclude-subs", "-e", help="Exclude specific subscriptions", metavar=""
    ),
):
    """Authenticate and run with Azure CLI credentials"""
    log.info("Attempting to login with Azure CLI credentials...")
    cred = AzureCliCredential()

    ctx.obj["ctx"] = CollectorContext(
        cred, cloud._cloud, mode, backfill, include_subs, exclude_subs
    )
