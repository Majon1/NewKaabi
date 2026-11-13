"""Play several practice weeks and keep their scores.

Practice only. The same seed and level can be played again. This script never
starts a ranking run and never joins the final. It deletes only the practice
worlds it created, and only after their reveal is saved.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from spread.client import Client, SpreadError
from spread.starter import report

ROOT = Path(__file__).resolve().parent
DEFAULT_SEEDS = ("spread-1", "setup-check", "harness-3")


def load_dotenv(path: Path) -> None:
    """Read KEY=value lines into the process. Values already set in the environment stay."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def practice_summary(client: Client, world_id: str) -> dict | None:
    for summary in client.worlds():
        if summary["worldId"] == world_id:
            return summary
    return None


def safe_delete(client: Client, world_id: str) -> None:
    """Delete one practice world this process created. Leave ranking runs and the final alone."""
    summary = practice_summary(client, world_id)
    if summary is None:
        return
    if summary["kind"] != "practice" or summary.get("boardRun") is not None:
        print(
            f"Leaving world {world_id} alone (kind={summary['kind']}, boardRun={summary.get('boardRun')}).",
            flush=True,
        )
        return
    client.world(world_id).delete()
    print(f"Deleted practice world {world_id}. The same seed can be played again.", flush=True)


def play_seed(level: int, seed: str, out_dir: Path, play) -> dict:
    client = Client()
    world = client.create(level=level, seed=seed, board=False)
    world_id = world.id
    saved = False
    try:
        summary = practice_summary(client, world_id)
        if summary is None or summary["kind"] != "practice" or summary.get("boardRun") is not None:
            raise SpreadError(0, f"World {world_id} is not an ordinary practice world. Stopping before any action.")
        print(f"Practice world {world_id}, seed {seed}, level {level}.", flush=True)
        play(world, level)
        score = world.score()
        report("End", score)
        print("Ranked." if score["ranked"] else "Not ranked.", flush=True)
        reveal_dir = out_dir / seed
        reveal_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "spread.reveal_csv", world_id, "--out", str(reveal_dir)],
            check=True,
        )
        saved = True
        return {
            "seed": seed,
            "worldId": world_id,
            "level": level,
            "marginCents": score["marginCents"],
            "revenueCents": score["revenueCents"],
            "marginShare": score["marginShare"],
            "wonOrders": score["wonOrders"],
            "onTimeShare": score["onTimeShare"],
            "undeliveredShare": score["undeliveredShare"],
            "ranked": score["ranked"],
            "reveal": str(reveal_dir),
            "error": "",
        }
    finally:
        # A week still in progress holds one of the eight slots. Once the reveal is saved,
        # the same seed can be played again, so the practice world can be removed.
        # An ended world whose reveal was not saved is left in place.
        summary = practice_summary(client, world_id)
        if saved or (summary is not None and not summary["ended"]):
            safe_delete(client, world_id)


SUMMARY_COLUMNS = [
    "seed", "worldId", "level", "marginCents", "revenueCents", "marginShare",
    "wonOrders", "onTimeShare", "undeliveredShare", "ranked", "reveal", "error",
]


def write_summary(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, SUMMARY_COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def print_table(rows: list[dict]) -> None:
    print("\nseed          margin €      on time   orders  ranked", flush=True)
    for row in rows:
        if row.get("error"):
            print(f"{row['seed']:<12}  failed: {row['error']}", flush=True)
            continue
        margin = row["marginCents"] / 100
        print(
            f"{row['seed']:<12}  {margin:>10,.2f}   {row['onTimeShare']:7.1%}   {row['wonOrders']:>6}  {row['ranked']}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Play practice weeks and save margin, on-time share, and reveal CSVs.")
    parser.add_argument("--level", type=int, choices=range(1, 9), default=2, help="practice level, 1 to 8 (default 2)")
    parser.add_argument("--seed", action="append", dest="seeds", help="practice seed; repeat for several. Default: three fixed seeds")
    parser.add_argument("--jobs", type=int, default=1, help="practice worlds at once, 1 to 4 (default 1)")
    parser.add_argument("--system", choices=("starter", "dispatch"), default="dispatch", help="which practice policy to run (default dispatch)")
    parser.add_argument("--out", type=Path, help="folder for the summary and reveal CSVs")
    parser.add_argument("--board", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--final", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.board or args.final:
        sys.exit("This harness plays practice worlds only. Ranking runs and the final are not started from here.")
    if not 1 <= args.jobs <= 4:
        sys.exit("--jobs must be from 1 to 4, so at least four practice slots stay free.")

    load_dotenv(ROOT / ".env")
    seeds = args.seeds or list(DEFAULT_SEEDS)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.system == "starter":
        from spread.starter import play_practice as play
    else:
        from dispatch import play_practice as play
    out_dir = args.out or ROOT / "runs" / f"level-{args.level}-{args.system}-{stamp}"

    client = Client()
    open_worlds = [world for world in client.worlds() if not world["ended"]]
    if len(open_worlds) + min(args.jobs, len(seeds)) > 8:
        sys.exit(
            f"{len(open_worlds)} practice worlds are already open, and a team can hold 8. "
            "End some before starting this many at once."
        )

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(play_seed, args.level, seed, out_dir, play): seed for seed in seeds}
        for future in as_completed(futures):
            seed = futures[future]
            try:
                rows.append(future.result())
            except (SpreadError, subprocess.CalledProcessError, OSError) as error:
                print(f"Seed {seed} failed: {error}", file=sys.stderr, flush=True)
                rows.append({"seed": seed, "error": str(error)})

    rows.sort(key=lambda row: seeds.index(row["seed"]))
    summary_path = out_dir / "summary.csv"
    write_summary(summary_path, rows)
    print_table(rows)
    print(f"\nSummary: {summary_path}", flush=True)


if __name__ == "__main__":
    main()
