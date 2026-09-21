"""Terminal UI: watch a document move through OCR/VLM -> classify -> extract
-> validate live, no browser required.

    python tui.py path/to/document.pdf
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from docket.pipeline import process

console = Console()


def _describe_stage(stage: str, payload) -> str:
    if stage == "ocr":
        return f"[green]OK[/] OCR/VLM — via [bold]{payload.method}[/] ({len(payload.text)} chars)"
    if stage == "classify":
        c = payload
        return f"[green]OK[/] Classify — [bold]{c.type_name}[/] via {c.method} ({c.confidence:.0%} confidence)"
    if stage == "extract":
        if payload is None:
            return "[red]FAIL[/] Extract — failed to produce valid structured output"
        return "[green]OK[/] Extract — schema validated"
    if stage == "validate":
        issues = payload or []
        errors = [i for i in issues if i.severity == "error"]
        if not issues:
            return "[green]OK[/] Validate — no issues"
        return f"[yellow]WARN[/] Validate — {len(errors)} error(s), {len(issues) - len(errors)} warning(s)"
    return stage


def run(path: Path) -> None:
    console.rule(f"[bold]docket[/] — {path.name}")

    lines: list[str] = []
    start = time.time()

    def on_stage(stage: str, payload: object) -> None:
        lines.append(_describe_stage(stage, payload))
        console.print(lines[-1])

    with console.status("[bold cyan]Running pipeline...", spinner="dots"):
        result = process(path, on_stage=on_stage)

    elapsed = time.time() - start
    console.print()

    summary = Table.grid(padding=(0, 2))
    summary.add_row("Doc type:", f"[bold]{result.classification.type_name}[/]")
    summary.add_row("Classified via:", result.classification.method)
    summary.add_row(
        "OCR method:",
        result.ocr_method
        + (" [yellow](re-read by VLM after validation failed)[/]" if result.escalated_to_vlm else ""),
    )
    summary.add_row("Extract attempts:", str(result.extract_attempts))
    summary.add_row("LLM calls:", f"{result.llm_calls} (~{result.llm_estimated_tokens} tokens)")
    summary.add_row("Elapsed:", f"{elapsed:.1f}s")
    console.print(Panel(summary, title="Summary", border_style="cyan"))

    if result.needs_review:
        reasons = "\n".join(f"• {r}" for r in result.review_reasons)
        console.print(
            Panel(
                f"[yellow]Queued for human review:[/]\n{reasons}",
                title="Review",
                border_style="yellow",
            )
        )

    if result.extracted is not None:
        console.print(Panel(JSON.from_data(result.extracted), title="Extracted fields", border_style="green"))
    else:
        console.print(Panel("[red]No structured output extracted.[/]", title="Extracted fields", border_style="red"))

    if result.validation_issues:
        table = Table(title="Validation issues", show_lines=False)
        table.add_column("Severity")
        table.add_column("Field")
        table.add_column("Message")
        for issue in result.validation_issues:
            style = "red" if issue.severity == "error" else "yellow"
            table.add_row(f"[{style}]{issue.severity}[/]", issue.field, issue.message)
        console.print(table)
    else:
        console.print(Panel("[green]All business rules passed.[/]", title="Validation", border_style="green"))


def main() -> None:
    if len(sys.argv) != 2:
        console.print("usage: python tui.py <path-to-document>", style="red")
        raise SystemExit(1)
    run(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
