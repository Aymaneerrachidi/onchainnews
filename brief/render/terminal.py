from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

from brief.models import Brief
from brief.render.markdown import render_markdown


def render_terminal(brief: Brief, console: Console | None = None) -> None:
    target = console or Console()
    content = render_markdown(brief)
    # Token symbols and names are untrusted external text. Preserve them on
    # UTF-8 terminals and replace only glyphs unsupported by legacy consoles.
    encoding = target.encoding or "utf-8"
    safe_content = content.encode(encoding, errors="replace").decode(encoding)
    target.print(Markdown(safe_content))
