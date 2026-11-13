"""How many cars of a week could go in pairs: car A's delivery near car B's pickup, one driver for both.

Usage: python analysis/pairs.py runs/reveal-<world id> [--near-km 30] [--max-drive 180]

Uses every price request of the week (what we could have won), single-car requests only. A pair A -> B
counts when:
- B's pickup is within --near-km (straight line) of A's delivery;
- both cars are known before the driver sets off with A (B's request has been answered);
- the driver can collect B after delivering A and deliver it by B's deadline, by the road minutes;
- a same-day car never waits for its partner; another car waits at most --wait minutes past its ready time;
- the driver idles at most --idle minutes between delivering A and collecting B;
- each drive is at most --max-drive minutes, and the whole gig at most 12 hours of driving.
Deadlines: each request's earliest option (the strictest case) and latest option are both tried.
Opening hours are ignored, so this is an upper bound on what the world would accept.
"""
import argparse
import math

import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("folder")
ap.add_argument("--near-km", type=float, default=30)
ap.add_argument("--max-drive", type=int, default=180)
ap.add_argument("--idle", type=int, default=60)
args = ap.parse_args()

d = args.folder
req = pd.read_csv(f"{d}/requests.csv")
legs = pd.read_csv(f"{d}/legs.csv")
places = pd.read_csv(f"{d}/places.csv").set_index("id")
roads = pd.read_csv(f"{d}/roads.csv").set_index(["from", "to"])


def km(a, b):
    pa, pb = places.loc[a], places.loc[b]
    dlat, dlon = math.radians(pb.lat - pa.lat), math.radians(pb.lon - pa.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(pa.lat)) * math.cos(math.radians(pb.lat)) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


one = req[req.legs == 1].set_index("id")
leg0 = legs[legs.leg == 0].set_index("requestId")
cars = []
for rid, r in one.iterrows():
    l = leg0.loc[rid]
    options = [(k, r[f"{k}.deadline"]) for k in ("same_day", "next_day", "within_3_days") if not pd.isna(r[f"{k}.deadline"])]
    p, q = l["pickup.placeId"], l["delivery.placeId"]
    cars.append(dict(id=rid, p=p, q=q, ready=int(l["pickup.window.from"]), known=int(r.respondBy),
                     drive=int(roads.loc[(p, q), "minutes"]), first=options[0], last=options[-1]))
cars = [c for c in cars if c["drive"] <= args.max_drive]
near = {}
for c in cars:
    for o in cars:
        if c is not o and (c["q"], o["p"]) not in near:
            near[(c["q"], o["p"])] = km(c["q"], o["p"]) <= args.near_km


def pairs(wait: int, option: str) -> list:
    found = []
    for a in cars:
        kind_a, dead_a = a[option]
        for b in cars:
            if a is b or not near[(a["q"], b["p"])]:
                continue
            kind_b, dead_b = b[option]
            hop = int(roads.loc[(a["q"], b["p"]), "minutes"])
            # The driver sets off with A when A is ready and both requests are answered; A may wait unless same-day.
            start = max(a["ready"], a["known"], b["known"])
            if start - a["ready"] > (0 if kind_a == "same_day" else wait):
                continue
            at_b = start + a["drive"] + hop
            if at_b - a["drive"] - hop > dead_a - a["drive"]:  # A must still arrive by its deadline
                continue
            collect = max(at_b, b["ready"])
            if collect - at_b > args.idle:  # the driver would idle too long
                continue
            if collect - b["ready"] > (0 if kind_b == "same_day" else wait):
                continue
            if collect + b["drive"] > dead_b or start + a["drive"] > dead_a:
                continue
            if a["drive"] + hop + b["drive"] > 12 * 60:
                continue
            found.append((a["id"], b["id"]))
    return found


def matched(found: list) -> int:
    """Cars in a greedy one-to-one matching: each car in at most one pair."""
    used, n = set(), 0
    for a, b in found:
        if a not in used and b not in used:
            used |= {a, b}
            n += 2
    return n


print(f"single-car requests with a drive up to {args.max_drive} min: {len(cars)}")
print(f"{'deadline':<10}{'car may wait':>14}{'possible pairs':>16}{'cars in pairs':>15}{'share':>8}")
for option, label in (("first", "earliest"), ("last", "latest")):
    for wait in (0, 120, 240, 1440):
        found = pairs(wait, option)
        n = matched(found)
        print(f"{label:<10}{wait:>11} min{len(found):>16}{n:>15}{n / len(cars):>8.1%}")
