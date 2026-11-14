"""Rehearses the live final on a practice world: the clock moves by itself, at the final's speed.

A second thread advances a practice world in real time, by the final's schedule (a weekday hour from 06:00 to
22:00 takes 18 real seconds, a Saturday hour 6, an hour of the last Monday 9, every other hour 3), while
dispatch.play_final answers from the live stream exactly as it will in the final. Nothing calls advance
for the system, so it has to keep up with the clock.

Usage:
  python analysis/rehearse.py --seed tune-1 [--speed 2] [--start-day 3]

--start-day 3 starts on Thursday 00:00, as the test finals do; the days before pass at once and unanswered.
--speed 2 runs the clock twice as fast as the final, a harder test that also takes half the time.
At the end it prints the score and how many quotes the world refused (most often: sent too late).
Practice only: it never touches a ranking run or the final.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dispatch  # noqa: E402
from spread.client import Client  # noqa: E402

DAY = 24 * 60


def seconds_per_hour(minute: int) -> float:
    """The final's clock: real seconds for the world hour that holds `minute`."""
    day, hour = minute // DAY, minute % DAY // 60
    day_time = 6 <= hour < 22
    if day <= 4 and day_time:
        return 18
    if day == 5 and day_time:
        return 6
    if day == 7 and day_time:
        return 9
    return 3


def run_clock(world_id: str, ends_at: int, speed: float, stop: threading.Event) -> None:
    """Advances the world minute by minute in real time. Its own client: the kit's client is not shared across threads."""
    world = Client().world(world_id)
    minute = world.snapshot()["now"]
    owed = 0.0
    last = time.monotonic()
    while not stop.is_set() and minute < ends_at:
        time.sleep(0.05)
        now = time.monotonic()
        owed += (now - last) * speed
        last = now
        target = minute
        while target < ends_at and owed >= seconds_per_hour(target) / 60:
            owed -= seconds_per_hour(target) / 60
            target += 1
        if target > minute:
            world.advance(to=target)
            minute = target


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="tune-1")
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--start-day", type=int, default=3)
    ap.add_argument("--param", action="append", default=[])
    args = ap.parse_args()
    dispatch.load_env()
    params = dispatch.parse_params(args.param)
    client = Client()
    world = client.create(level=args.level, seed=args.seed)
    print(f"Rehearsal on practice world {world.id}, seed {args.seed}, level {args.level}, clock x{args.speed}.", flush=True)
    try:
        if args.start_day:
            world.advance(to=args.start_day * DAY)
        ends_at = world.snapshot()["endsAt"]
        stop = threading.Event()
        clock = threading.Thread(target=run_clock, args=(world.id, ends_at, args.speed, stop), daemon=True)
        started = time.monotonic()
        clock.start()
        dispatch.play_final(world, params, clock=lambda: world.snapshot()["now"])
        stop.set()
        clock.join(timeout=5)
        score = world.score()
        dispatch.report("End", score)
        print(f"Real time {(time.monotonic() - started) / 60:.1f} min. Ranked: {score['ranked']}.", flush=True)
    except BaseException:
        world.delete()
        print(f"Stopped; practice world {world.id} deleted.", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
