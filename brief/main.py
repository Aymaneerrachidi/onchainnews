from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from rich.console import Console
from rich.table import Table

from brief.config import load_settings
from brief.delivery import EmailDeliveryError, TelegramDeliveryError, send_email, send_telegram, write_html
from brief.engine import build_brief
from brief.interface import serve_interface
from brief.ledger import open_ledger
from brief.launch_collector import run_launch_collector
from brief.pulse import run_pulse
from brief.render.email import email_subject, render_email
from brief.render.html import render_html
from brief.render.payload import build_payload
from brief.render.telegram import render_telegram
from brief.render.terminal import render_terminal
from brief.watcher import run_watcher


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="solana-brief", description="Novelty-first Solana memecoin daily brief")
    result.add_argument("--config", default="config.toml", help="path to config.toml")
    sub = result.add_subparsers(dest="command")
    run = sub.add_parser("run", help="fetch, screen, render, and deliver today's brief")
    run.add_argument("--dry-run", action="store_true", help="do not update feature/return history")
    run.add_argument("--no-telegram", action="store_true", help="skip Telegram even when configured")
    run.add_argument("--no-email", action="store_true", help="skip email even when configured")
    sub.add_parser("status", help="show ledger statistics")
    prune = sub.add_parser("prune", help="delete archived HTTP bodies older than the retention window")
    prune.add_argument("--days", type=int, help="override run.archive_retention_days")
    prune.add_argument("--vacuum", action="store_true", help="rewrite the database file to reclaim space")
    unretire = sub.add_parser("unretire", help="allow a manually retired token to re-enter")
    unretire.add_argument("mint")
    watch = sub.add_parser("watch", help="manage the holder-snapshot watchlist")
    watch.add_argument("action", choices=("add", "remove", "list"))
    watch.add_argument("mint", nargs="?")
    watch.add_argument("--symbol", default="?")
    replay = sub.add_parser("replay", help="re-run an archived date against the current scoring logic")
    replay.add_argument("date", help="archive date in YYYY-MM-DD format")
    mark = sub.add_parser("mark", help="record whether a surfaced mint was traded or skipped")
    mark.add_argument("mint")
    mark.add_argument("decision", choices=("traded", "skipped"))
    sub.add_parser("weekly", help="print the current seven-day retrospective")
    watcher = sub.add_parser("watcher", help="poll flagged tokens and push material Telegram alerts")
    watcher.add_argument("--once", action="store_true", help="run one polling cycle")
    watcher.add_argument("--interval", type=int, help="seconds between polls")
    watcher.add_argument("--max-cycles", type=int, help=argparse.SUPPRESS)
    sub.add_parser("pulse", help="run one hourly runner pass check and optional X alert")
    collector = sub.add_parser("collector", help="continuously index new Pump.fun launches from Helius")
    collector.add_argument("--max-events", type=int, help=argparse.SUPPRESS)
    interface_parser = sub.add_parser("interface", help="serve and open the local visual brief interface")
    interface_parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    interface_parser.add_argument("--port", type=int, default=8765)
    interface_parser.add_argument("--no-browser", action="store_true")
    return result


async def _failure_alert(message: str) -> None:
    try:
        await send_telegram([f"SOLANA BRIEF FAILED\n{message[:500]}"])
    except Exception:
        logging.getLogger("brief").exception("Could not send Telegram failure alert")


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    logging.basicConfig(
        level=getattr(logging, str(settings.get("run", "log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx includes query parameters in its INFO line; a Helius API key is a
    # query parameter, so keep transport logging below the secret-bearing level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    console = Console()
    ledger = open_ledger(settings)
    telegram_enabled = bool(settings.get("delivery", "telegram_enabled", False)) and not args.no_telegram
    try:
        brief = await build_brief(settings, ledger, commit=not args.dry_run)
        render_terminal(brief, console)
        if settings.get("delivery", "html_enabled", True):
            html_path = settings.path("run", "html_path")
            write_html(html_path, render_html(brief))
            console.print(f"[dim]HTML written to {html_path}[/dim]")
        if settings.get("run", "json_path"):
            json_path = settings.path("run", "json_path")
            write_html(json_path, json.dumps(build_payload(brief, settings), indent=1))
            console.print(f"[dim]Site snapshot written to {json_path}[/dim]")
        if telegram_enabled and not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
            # A missing credential must not lose the morning report that already rendered.
            console.print("[yellow]Telegram enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are unset; skipping delivery.[/yellow]")
        elif telegram_enabled:
            messages = render_telegram(
                brief,
                digest=bool(settings.get("delivery", "telegram_digest", True)),
                report_url=str(settings.get("delivery", "report_url", "") or ""),
            )
            try:
                await send_telegram(messages)
                console.print(f"[dim]Telegram digest delivered ({len(messages)} message(s)).[/dim]")
            except TelegramDeliveryError as exc:
                # The public report is already rendered. Keep web publishing
                # independent from a temporary Telegram/API failure.
                logging.getLogger("brief.delivery").error("Telegram digest failed: %s", exc)
                console.print("[yellow]Telegram delivery failed; the web report will still publish.[/yellow]")
        elif not args.no_telegram:
            console.print("[dim]Telegram disabled; set delivery.telegram_enabled=true after configuring .env.[/dim]")
        if settings.get("delivery", "email_enabled", False) and args.no_email:
            console.print("[dim]Email disabled for this run by --no-email.[/dim]")
        elif settings.get("delivery", "email_enabled", False):
            recipients = list(settings.get("delivery", "email_to", []) or [])
            provider = str(settings.get("delivery", "email_provider", "resend") or "resend").strip().lower()
            required_key = "BREVO_API_KEY" if provider == "brevo" else "RESEND_API_KEY"
            if not os.getenv(required_key) or not recipients:
                # A missing credential must not lose the morning report that already rendered.
                console.print(f"[yellow]Email enabled but {required_key}/email_to unset; skipping delivery.[/yellow]")
            else:
                try:
                    count = await send_email(settings, email_subject(brief, settings), render_email(brief, settings))
                    console.print(f"[dim]Email digest delivered ({count} recipient(s)).[/dim]")
                except EmailDeliveryError as exc:
                    # The public report is already rendered. Keep web publishing
                    # independent from a temporary email/API failure.
                    logging.getLogger("brief.delivery").error("Email delivery failed: %s", exc)
                    console.print("[yellow]Email delivery failed; the web report will still publish.[/yellow]")
        return 0
    except Exception as exc:
        logging.getLogger("brief").exception("Daily brief failed")
        console.print(f"[bold red]Brief failed:[/bold red] {exc}")
        if telegram_enabled and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            await _failure_alert(str(exc))
        return 1
    finally:
        ledger.close()


async def replay(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    timezone = ZoneInfo(str(settings.get("run", "timezone", "UTC")))
    try:
        replay_day = date.fromisoformat(args.date)
    except ValueError:
        Console().print("[red]Replay date must use YYYY-MM-DD.[/red]")
        return 2
    replay_time = datetime.combine(replay_day, time(6, 45), tzinfo=timezone)
    ledger = open_ledger(settings)
    try:
        brief = await build_brief(
            settings, ledger, commit=False, now=replay_time, replay_date=args.date
        )
        render_terminal(brief)
        output = settings.path("run", "html_path").with_name(f"replay-{args.date}.html")
        write_html(output, render_html(brief))
        Console().print(f"[dim]Deterministic replay written to {output}[/dim]")
        return 0
    except Exception as exc:
        logging.getLogger("brief").exception("Replay failed")
        Console().print(f"[bold red]Replay failed:[/bold red] {exc}")
        return 1
    finally:
        ledger.close()


def status(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    try:
        table = Table(title="Solana Brief Ledger")
        table.add_column("Metric")
        table.add_column("Count", justify="right")
        for name, count in ledger.stats().items():
            table.add_row(name.replace("_", " ").title(), str(count))
        Console().print(table)
        return 0
    finally:
        ledger.close()


def prune(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    console = Console()
    try:
        now = datetime.now(ZoneInfo(str(settings.get("run", "timezone", "UTC"))))
        days = args.days if args.days is not None else int(settings.get("run", "archive_retention_days", 14))
        before = ledger.database_bytes()
        compacted = ledger.compact_archive()
        removed = ledger.prune_archive(now, days, vacuum=args.vacuum)
        after = ledger.database_bytes()
        console.print(f"Compressed {compacted:,} previously uncompressed responses.")
        console.print(f"Removed {removed:,} archived responses older than {days} days.")
        console.print(f"Database {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB.")
        if not args.vacuum:
            console.print("[dim]Run with --vacuum to reclaim the freed pages on disk.[/dim]")
        elif not ledger.checkpoint():
            console.print(
                "[yellow]Another process is attached to the database, so the file could not be "
                "truncated. Stop the collector and the interface, then run this again.[/yellow]"
            )
        return 0
    finally:
        ledger.close()


def unretire(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    try:
        changed = ledger.unretire(args.mint)
        Console().print("Token un-retired." if changed else "Mint is not in the feature ledger.")
        return 0 if changed else 1
    finally:
        ledger.close()


def mark(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    try:
        now = datetime.now(ZoneInfo(str(settings.get("run", "timezone", "UTC"))))
        ledger.mark_trade(args.mint, args.decision, now)
        Console().print(f"Marked {args.mint} as {args.decision}.")
        return 0
    finally:
        ledger.close()


def weekly(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    try:
        now = datetime.now(ZoneInfo(str(settings.get("run", "timezone", "UTC"))))
        notes = ledger.weekly_retrospective(now)
        console = Console()
        console.print("[bold]WEEKLY RETROSPECTIVE[/bold]")
        for note in notes or ["Not enough matured 72h observations yet."]:
            console.print(f"- {note}")
        return 0
    finally:
        ledger.close()


async def watcher(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    try:
        interval = args.interval or int(settings.get("intelligence", "watcher_interval_seconds", 300))
        if interval < 15:
            Console().print("[red]Watcher interval must be at least 15 seconds.[/red]")
            return 2
        cycles = 1 if args.once else args.max_cycles
        await run_watcher(settings, ledger, interval_seconds=interval, max_cycles=cycles)
        return 0
    except Exception as exc:
        logging.getLogger("brief.watcher").exception("Watcher failed")
        Console().print(f"[bold red]Watcher failed:[/bold red] {exc}")
        return 1
    finally:
        ledger.close()


async def pulse(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    logging.basicConfig(
        level=getattr(logging, str(settings.get("run", "log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ledger = open_ledger(settings)
    console = Console()
    try:
        result = await run_pulse(settings, ledger)
        console.print(f"[dim]Pulse checked {result.checked} runner(s). State: {result.state_path}[/dim]")
        if result.latest_written:
            console.print(f"[dim]Live site snapshot refreshed: {result.latest_written}[/dim]")
        if not result.triggers:
            console.print("[dim]No runner crossed the sustained-pass threshold.[/dim]")
        for trigger in result.triggers:
            status = f"posted to X as {trigger.x_post_id}" if trigger.x_post_id else (trigger.error or "image generated")
            console.print(
                f"[green]${trigger.candidate.token.symbol} sustained runner trigger:[/green] "
                f"{len(trigger.passes)} pass(es), {status}"
            )
        return 0
    except Exception as exc:
        logging.getLogger("brief.pulse").exception("Pulse failed")
        console.print(f"[bold red]Pulse failed:[/bold red] {exc}")
        return 1
    finally:
        ledger.close()


async def collector(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    logging.basicConfig(
        level=getattr(logging, str(settings.get("run", "log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ledger = open_ledger(settings)
    try:
        await run_launch_collector(settings, ledger, max_events=args.max_events)
        return 0
    except Exception as exc:
        logging.getLogger("brief.launch_collector").exception("Launch collector failed")
        Console().print(f"[bold red]Launch collector failed:[/bold red] {exc}")
        return 1
    finally:
        ledger.close()


def interface(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    logging.basicConfig(
        level=getattr(logging, str(settings.get("run", "log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        serve_interface(settings, host=args.host, port=args.port, open_browser=not args.no_browser)
        return 0
    except (OSError, ValueError) as exc:
        Console().print(f"[bold red]Interface could not start:[/bold red] {exc}")
        return 1


def watch(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = open_ledger(settings)
    console = Console()
    try:
        if args.action == "list":
            table = Table(title="Holder Snapshot Watchlist")
            table.add_column("Symbol")
            table.add_column("Mint")
            table.add_column("Reason")
            for row in ledger.watched():
                table.add_row(row["symbol"], row["mint"], row["reason"])
            console.print(table)
            return 0
        if not args.mint:
            console.print("[red]A mint is required for watch add/remove.[/red]")
            return 2
        if args.action == "add":
            from datetime import datetime
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(str(settings.get("run", "timezone", "UTC"))))
            ledger.add_watch(args.mint, args.symbol, now)
            console.print(f"Watching {args.symbol.upper()} ({args.mint}).")
            return 0
        changed = ledger.remove_watch(args.mint)
        console.print("Removed from watchlist." if changed else "Mint is not in the watchlist.")
        return 0 if changed else 1
    finally:
        ledger.close()


def cli() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    args.command = args.command or "run"
    if args.command == "run":
        if not hasattr(args, "dry_run"):
            args.dry_run = False
            args.no_telegram = False
        code = asyncio.run(run(args))
    elif args.command == "status":
        code = status(args)
    elif args.command == "prune":
        code = prune(args)
    elif args.command == "unretire":
        code = unretire(args)
    elif args.command == "watch":
        code = watch(args)
    elif args.command == "replay":
        code = asyncio.run(replay(args))
    elif args.command == "mark":
        code = mark(args)
    elif args.command == "weekly":
        code = weekly(args)
    elif args.command == "watcher":
        code = asyncio.run(watcher(args))
    elif args.command == "pulse":
        code = asyncio.run(pulse(args))
    elif args.command == "collector":
        code = asyncio.run(collector(args))
    else:
        code = interface(args)
    raise SystemExit(code)


if __name__ == "__main__":
    cli()
