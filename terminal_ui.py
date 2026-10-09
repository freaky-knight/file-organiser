"""Dependency-free, terminal-aware presentation helpers."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import TextIO

COLORS = {
    "cyan": "\033[96m",
    "teal": "\033[36m",
    "blue": "\033[36m",
    "white": "\033[97m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "muted": "\033[90m",
    "selected": "\033[1;96m",
}
RESET = "\033[0m"
TAGLINE = "Organize with clarity. Preview first. Keep every move reversible."


def terminal_width(fallback: int = 80) -> int:
    """Return a conservative width for the active terminal."""
    return max(16, min(100, shutil.get_terminal_size((fallback, 24)).columns))


def supports_color(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    return (
        "NO_COLOR" not in os.environ
        and hasattr(stream, "isatty")
        and stream.isatty()
        and os.environ.get("TERM", "") != "dumb"
    )


def supports_unicode(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    try:
        "┌─┐│└┘✓✗⚠→•ℹ━▣⌕▤⧉↺⚙×".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def paint(value: str, color: str, stream: TextIO | None = None) -> str:
    if not supports_color(stream) or color not in COLORS:
        return value
    return f"{COLORS[color]}{value}{RESET}"


def icon(kind: str, stream: TextIO | None = None) -> str:
    unicode_icons = supports_unicode(stream)
    icons = {
        "success": ("✓", "[OK]"),
        "failure": ("✗", "[X]"),
        "warning": ("⚠", "[!]"),
        "action": ("→", "->"),
        "item": ("•", "*"),
        "info": ("ℹ", "[i]"),
        "folder": ("▣", "[+]"),
        "scan": ("⌕", "[S]"),
        "search": ("⌕", "[?]"),
        "stats": ("▤", "[#]"),
        "duplicate": ("⧉", "[=]"),
        "history": ("↺", "[H]"),
        "settings": ("⚙", "[C]"),
        "exit": ("×", "[0]"),
    }
    unicode_icon, plain_icon = icons[kind]
    return unicode_icon if unicode_icons else plain_icon


def divider(label: str | None = None, stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    width = terminal_width() - 2
    rule = "━" if supports_unicode(stream) else "-"
    if not label:
        return paint(rule * width, "teal", stream)
    plain_label = label.strip()
    if not supports_unicode(stream):
        plain_label = plain_label.replace("·", "|")
    if len(plain_label) + 2 > width:
        plain_label = plain_label[:max(0, width - 5)] + "..."
    safe_label = f" {plain_label} "
    remaining = max(0, width - len(safe_label))
    left = remaining // 2
    right = remaining - left
    return paint(rule * left + safe_label + rule * right, "teal", stream)


def panel(title: str, rows: list[str], stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    unicode_borders = supports_unicode(stream)
    width = terminal_width()
    inner_width = max(8, width - 6)
    lines: list[str] = []
    for row in rows:
        lines.extend(
            textwrap.wrap(
                row,
                width=inner_width,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
            ) or [""]
        )
    content_width = min(inner_width, max([len(title), *(len(line) for line in lines)]))
    content_width = max(8, content_width)
    if unicode_borders:
        top_left, top_right, side, middle_left, middle_right, bottom_left, bottom_right, horizontal = (
            "┌", "┐", "│", "├", "┤", "└", "┘", "─"
        )
    else:
        top_left, top_right, side, middle_left, middle_right, bottom_left, bottom_right, horizontal = (
            "+", "+", "|", "+", "+", "+", "+", "-"
        )
    border = horizontal * (content_width + 2)
    title_text = title[:content_width]
    rendered = [
        paint(f"{top_left}{border}{top_right}", "teal", stream),
        f"{paint(side, 'teal', stream)} {paint(title_text, 'cyan', stream)}{' ' * (content_width - len(title_text))} {paint(side, 'teal', stream)}",
        paint(f"{middle_left}{border}{middle_right}", "teal", stream),
    ]
    rendered.extend(
        f"{paint(side, 'teal', stream)} {line:<{content_width}} {paint(side, 'teal', stream)}"
        for line in lines
    )
    rendered.append(paint(f"{bottom_left}{border}{bottom_right}", "teal", stream))
    return "\n".join(rendered)


def table(
    headers: list[str],
    rows: list[list[str]],
    stream: TextIO | None = None,
    *,
    styles: dict[int, str] | None = None,
    highlight_row: int | None = None,
) -> str:
    """Render a compact table, falling back to readable stacked rows when tight."""
    stream = stream or sys.stdout
    if not headers:
        return ""
    width = terminal_width()
    rows = [[str(value) for value in row] for row in rows]
    styles = styles or {}
    if width < 54 or len(headers) > 4:
        result: list[str] = []
        for index, row in enumerate(rows):
            result.append(divider(f"ITEM {index + 1}", stream))
            for column, header in enumerate(headers):
                result.append(f"{paint(header, 'muted', stream)}:")
                value = row[column] if column < len(row) else ""
                wrapped = textwrap.wrap(
                    value, width=max(8, width - 4), break_long_words=True,
                    break_on_hyphens=False,
                ) or [""]
                style = "selected" if index == highlight_row else styles.get(column)
                result.extend(paint(line, style, stream) if style else line for line in wrapped)
        return "\n".join(result) or paint("(no results)", "muted", stream)

    available = width - (len(headers) * 3) - 1
    natural = [
        min(max([len(headers[index]), *(len(row[index]) if index < len(row) else 0 for row in rows)]), 36)
        for index in range(len(headers))
    ]
    widths = natural[:]
    while sum(widths) > available:
        largest = max(range(len(widths)), key=widths.__getitem__)
        if widths[largest] <= 8:
            break
        widths[largest] -= 1

    def cell(value: str, cell_width: int) -> str:
        if len(value) > cell_width:
            ellipsis = "…" if supports_unicode(stream) else "..."
            if "/" in value or "\\" in value:
                return ellipsis + value[-(cell_width - len(ellipsis)):]
            return value[:max(1, cell_width - len(ellipsis))] + ellipsis
        return value.ljust(cell_width)

    separator = " │ " if supports_unicode(stream) else " | "
    rule = "─" if supports_unicode(stream) else "-"
    lines = [
        separator.join(paint(cell(header, widths[index]), "cyan", stream) for index, header in enumerate(headers)),
        paint("-+-".join(rule * column_width for column_width in widths), "teal", stream),
    ]
    for row_index, row in enumerate(rows):
        selected_style = "selected" if row_index == highlight_row else None
        values = []
        for column in range(len(headers)):
            value = cell(row[column] if column < len(row) else "", widths[column])
            style = selected_style or styles.get(column)
            values.append(paint(value, style, stream) if style else value)
        lines.append(separator.join(values))
    if not rows:
        lines.append(paint("(no results)", "muted", stream))
    return "\n".join(lines)


def status(message: str, kind: str = "info", stream: TextIO | None = None) -> str:
    colors = {"success": "green", "warning": "yellow", "failure": "red", "info": "cyan"}
    if kind not in colors:
        raise ValueError(f"unsupported status kind: {kind}")
    return paint(f"{icon(kind, stream)}  {message}", colors[kind], stream)


def header(version: str, stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    width = terminal_width()
    if supports_unicode(stream):
        logo = ["   ┌─────────┐", "   │ ▣   ▣   │", "   │  FILES  │", "   └─────────┘"]
    else:
        logo = ["   +---------+", "   | [ ] [ ] |", "   |  FILES  |", "   +---------+"]
    logo = [paint(line, "teal", stream) for line in logo]
    title = paint("FILE ORGANIZER", "cyan", stream)
    version_line = paint(f"v{version}", "muted", stream)
    if width < 58:
        tagline = textwrap.wrap(TAGLINE, width=max(12, width - 4))
        lines = [*logo, f"  {title}  {version_line}"]
        lines.extend(f"  {paint(line, 'white', stream)}" for line in tagline)
    else:
        details = [
            f"   {title}",
            f"   {version_line}",
            f"   {paint(TAGLINE, 'white', stream)}",
        ]
        lines = [
            f"{logo[0]}   {details[0]}",
            f"{logo[1]}   {details[1]}",
            f"{logo[2]}   {details[2]}",
            logo[3],
        ]
    lines.append(divider("FILE SYSTEM CONSOLE", stream))
    return "\n".join(lines)


def banner(version: str, stream: TextIO | None = None) -> str:
    return header(version, stream)


def progress(stage: str, completed: int, total: int, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    if not supports_color(stream) or total <= 0:
        return
    width = min(24, max(8, terminal_width() - 36))
    filled = min(width, int(width * completed / total))
    bar = f"{'=' * filled}{' ' * (width - filled)}"
    stream.write(f"\r{paint(stage, 'cyan', stream)} [{paint(bar, 'teal', stream)}] {completed}/{total}")
    if completed >= total:
        stream.write("\n")
    stream.flush()


def progress_count(stage: str, count: int, *, final: bool = False, stream: TextIO | None = None) -> None:
    """Render an observed count without implying a percentage or total."""
    stream = stream or sys.stdout
    if not supports_color(stream) or count < 1:
        return
    if count != 1 and count % 100 != 0 and not final:
        return
    stream.write(f"\r{paint(stage, 'cyan', stream)}: {count} files discovered")
    if final:
        stream.write("\n")
    stream.flush()
