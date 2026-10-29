import argparse
import os
import sys
from pathlib import Path

from . import __version__, report
from .planner import build_plan

PLEDGE_PER_DEV = 2000.0


def _parser():
    p = argparse.ArgumentParser(
        prog="sponsor-plan",
        description="Read lockfiles, find which dependencies accept funding and split a yearly budget between them.")
    p.add_argument("paths", nargs="*", default=["."], help="lockfiles or folders to scan (default: current folder)")
    p.add_argument("-b", "--budget", type=float, help="yearly budget to split")
    p.add_argument("--devs", type=int, help="developers on the team; budget = devs x --per-dev (Open Source Pledge style)")
    p.add_argument("--per-dev", type=float, default=PLEDGE_PER_DEV, help=f"yearly amount per developer with --devs (default {PLEDGE_PER_DEV:.0f})")
    p.add_argument("-c", "--currency", default="USD", help="label for amounts (no conversion), default USD")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                   help="skip packages by name, e.g. '@mycompany/*' (repeatable)")
    p.add_argument("--skip-recipient", action="append", default=[], metavar="GLOB",
                   help="never pay funding pages matching this, e.g. 'github.com/sponsors/myorg' (your own projects don't count for the Pledge)")
    p.add_argument("--no-dev", action="store_true", help="ignore development-only dependencies")
    p.add_argument("--exponent", type=float, default=0.7,
                   help="1 = pay in proportion to reliance, lower = flatter, spreads money to smaller projects (default 0.7)")
    p.add_argument("--min", type=float, default=5.0, dest="min_amount",
                   help="smallest worthwhile yearly payment; smaller ones are dropped and re-split (default 5)")
    p.add_argument("--max-share", type=float, default=0.3, help="largest fraction one recipient may get (default 0.3)")
    p.add_argument("--top", type=int, help="only the N most relied-on recipients")
    p.add_argument("-f", "--format", choices=["text", "md", "csv", "json"], default="text")
    p.add_argument("-l", "--lang", choices=["en", "ro"], default="en")
    p.add_argument("-o", "--output", help="write the report to a file")
    p.add_argument("--unfunded", type=int, default=10, help="how many heavily used but unfunded packages to list (default 10)")
    p.add_argument("--offline", action="store_true", help="use only the local cache, no network")
    p.add_argument("--no-repo-lookup", action="store_true", help="skip FUNDING.yml lookups in package repositories")
    p.add_argument("--cache", default=os.environ.get("SPONSOR_PLAN_CACHE") or str(Path.home() / ".cache" / "sponsor-planner" / "cache.json"),
                   help="cache file for registry answers (kept 7 days)")
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    a = _parser().parse_args(argv)
    budget = a.budget
    if budget is None and a.devs:
        budget = a.devs * a.per_dev
    if budget is None or budget <= 0:
        print("error: give a positive --budget, or --devs N (N x --per-dev)", file=sys.stderr)
        return 2
    if not 0 < a.max_share <= 1 or a.exponent <= 0:
        print("error: --max-share must be in (0, 1] and --exponent > 0", file=sys.stderr)
        return 2

    def progress(done, total):
        if sys.stderr.isatty():
            print(f"\rchecking funding {done}/{total}", end="", file=sys.stderr, flush=True)
            if done == total:
                print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
        elif done == 1 and not a.offline:
            print(f"checking {total} packages (first run can take a minute, answers are cached)...", file=sys.stderr)

    try:
        plan = build_plan(a.paths, budget, a.currency, a.exclude, not a.no_dev, a.exponent, a.min_amount,
                          a.max_share, a.top, a.skip_recipient, cache_path=a.cache, offline=a.offline,
                          repo_lookup=not a.no_repo_lookup, progress=progress)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = {"text": lambda: report.render_text(plan, a.lang, a.unfunded),
            "md": lambda: report.render_text(plan, a.lang, a.unfunded, markdown=True),
            "csv": lambda: report.render_csv(plan),
            "json": lambda: report.render_json(plan)}[a.format]()
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"wrote {a.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0 if plan.shares else 1
