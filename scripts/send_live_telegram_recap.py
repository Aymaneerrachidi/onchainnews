"""Send the final enriched daily snapshot to Telegram with inline filters."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.delivery import send_telegram  # noqa: E402
from brief.render.telegram_interactive import render_snapshot_message  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--snapshot", default="web/data/latest.json")
    result.add_argument("--report-url", default="https://onchainnews-rho.vercel.app")
    return result


async def run() -> None:
    args = parser().parse_args()
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    await send_telegram([render_snapshot_message(snapshot, report_url=args.report_url)])


if __name__ == "__main__":
    asyncio.run(run())
