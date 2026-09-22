"""Record the CLI's surfaces as SVG terminal transcripts.

    python scripts/record_demo.py                      # -> docs/demo/*.svg + *.txt
    python scripts/record_demo.py --scene governance   # just one
    python scripts/record_demo.py --width 120          # wider transcript

Why a script rather than an asciinema cast: the recording has to be *rebuildable*.
A hand-driven screen capture goes stale the first time the renderer changes and
nobody notices, whereas this runs the real pipeline through the real renderer, so
regenerating it is a one-liner and a drifted demo shows up as a diff. It is also
deterministic to produce in CI, which a keystroke recording is not.

Three scenes, because the project has three things worth looking at and one image
of all of them would be unreadable:

``demo``
    The answer path. A ranked answer, the validator's verdict under ``--explain``,
    a guardrail refusing an ambiguous question, and a failed run carrying
    machine-readable issue codes.
``governance``
    The same question asked by two principals, which is the only way to *see* that
    the row predicate is injected rather than requested, plus the audit chain
    verifying itself.
``evaluation``
    The ablation ladder, rendered from the runs already committed under
    ``evals/results/``. Deliberately not a fresh run: a recording is not a
    measurement, and re-running the suite here would either cost a provider's
    tokens or silently record the deterministic fallback as if it were the model.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

try:
    import semantic_query_engine  # noqa: F401
except ImportError:  # pragma: no cover - convenience fallback, not the primary path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule

from semantic_query_engine.cli import main as cli_main
from semantic_query_engine.cli.render import render
from semantic_query_engine.core.config import PROJECT_ROOT
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

DEMO_DIR = PROJECT_ROOT / "docs" / "demo"

# (flags shown in the prompt line, question, explain?)
FRAMES: list[tuple[str, str, bool]] = [
    ("", "Compare total revenue across all brands", False),
    ("--explain", "What is the stock depletion rate by SKU?", True),
    ("", "Show me the numbers", False),
    ("--explain", "What was the total revenue for brand Zzzz in 1999?", True),
]

# The published ladder: the four 118-case runs README.md quotes. Named rather than
# globbed, and deliberately not "the newest four" -- results/ also holds a 26-case
# `--difficulty` slice whose rungs read 4 points higher, and a transcript that
# silently swapped it in would show numbers no other artefact in the repo agrees with.
LADDER_RUNS = [
    "20260919T232715_gold_naive.json",
    "20260919T234317_gold_semantic.json",
    "20260920T000314_gold_validator.json",
    "20260920T002230_gold_full.json",
]


def prompt(console: Console, line: str) -> None:
    console.print(f"\n[bold green]$[/bold green] [cyan]{line}[/cyan]\n")


def run_cli(console: Console, argv: list[str]) -> None:
    """Invoke a real CLI command, with its output captured into ``console``.

    ``cli_main.out`` is the module-level Console every command prints through, so
    pointing it at the recording console records exactly what a user would see --
    as opposed to re-implementing each command's rendering here, which is the
    same two-code-paths mistake the CLI itself avoids by rendering ``to_dict()``.
    """
    prompt(console, "sqe " + " ".join(argv))
    command = typer.main.get_command(cli_main.app)
    saved = cli_main.out
    cli_main.out = console
    try:
        command.main(argv, standalone_mode=False)
    except SystemExit:  # `typer.Exit` carries the exit code; the transcript does not need it
        pass
    finally:
        cli_main.out = saved
    console.print()


def scene_demo(console: Console) -> None:
    pipeline = AnalyticsPipeline()
    for flags, question, explain in FRAMES:
        suffix = f" {flags}" if flags else ""
        prompt(console, f"sqe ask {question!r}{suffix}")
        result = pipeline.run(question)
        render(console, result.to_dict(), question=question, explain=explain)
        console.print()


def scene_governance(console: Console) -> None:
    run_cli(console, ["principals"])
    # The point of the pair: same question, same pipeline, different principal.
    # The unrestricted steward's total is the published baseline; analyst_north's
    # is smaller because a predicate it never asked for was injected into the SQL.
    run_cli(console, ["ask", "Total revenue by region"])
    run_cli(console, ["ask", "Total revenue by region", "--as", "analyst_north", "--explain"])
    run_cli(console, ["audit", "--verify"])


def scene_evaluation(console: Console) -> None:
    cli_main._ensure_evals_importable()
    from evals.harness import RESULTS_DIR, load_run
    from evals.report import render_funnel, render_ladder, render_strata

    runs = [load_run(RESULTS_DIR / name) for name in LADDER_RUNS]
    prompt(console, "sqe eval --baseline ladder")
    console.print(Rule("replayed from evals/results/ -- see the module docstring", style="dim"))
    console.print(Markdown(render_ladder(runs)))
    console.print()
    # The ladder answers "does the guardrail layer pay for itself"; the funnel and
    # the strata answer "where does it spend its rejections", which is the number
    # the IssueCode enum exists to make countable.
    console.print(Markdown(render_strata(runs[-1])))
    console.print()
    console.print(Markdown(render_funnel(runs[-1])))
    console.print()


SCENES: dict[str, Callable[[Console], None]] = {
    "demo": scene_demo,
    "governance": scene_governance,
    "evaluation": scene_evaluation,
}


def record(name: str, out_dir: Path, width: int) -> Path:
    # record=True buffers everything rendered so it can be replayed into SVG;
    # the real stdout is left alone so the script itself stays quiet.
    console = Console(record=True, width=width, file=open(os.devnull, "w", encoding="utf-8"))
    try:
        SCENES[name](console)
    finally:
        console.file.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{name}.svg"
    # export_text first, and without clearing: save_svg drains the record buffer,
    # so the order here is not cosmetic -- reversed, the transcript comes out empty.
    (out_dir / f"{name}.txt").write_text(console.export_text(clear=False), encoding="utf-8")
    console.save_svg(str(svg_path), title=f"sqe {name} -- governed NL to SQL")
    return svg_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEMO_DIR, help="Directory to write the transcripts into.")
    parser.add_argument("--width", type=int, default=100, help="Terminal width to render at.")
    parser.add_argument(
        "--scene",
        choices=[*SCENES, "all"],
        default="all",
        help="Which transcript to record. Default: all of them.",
    )
    args = parser.parse_args()

    names = list(SCENES) if args.scene == "all" else [args.scene]
    for name in names:
        svg_path = record(name, args.out, args.width)
        txt_path = svg_path.with_suffix(".txt")
        print(f"wrote {svg_path.relative_to(PROJECT_ROOT)} and {txt_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
