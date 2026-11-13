"""Which orders of a revealed week were late, and what they have in common.

Usage: python analysis/late.py runs/reveal-<world id>
"""
import sys

import pandas as pd

d = sys.argv[1]
req = pd.read_csv(f"{d}/requests.csv")
legs = pd.read_csv(f"{d}/legs.csv")
gigs = pd.read_csv(f"{d}/gigs.csv")
drivers = pd.read_csv(f"{d}/drivers.csv")
roads = pd.read_csv(f"{d}/roads.csv")
ev = pd.read_csv(f"{d}/events.csv", low_memory=False)

led = ev[ev.kind == "ledger"]
by = led.pivot_table(index="line.orderId", columns="line.kind", values="line.cents", aggfunc="sum").fillna(0)

o = req[req.outcome == "won"].set_index("id").copy()
o["price"] = o.apply(lambda r: r[f"{r.option}.quoteCents"], axis=1)
o["list"] = o.apply(lambda r: r[f"{r.option}.listPriceCents"], axis=1)
o = o.join(by, how="left").fillna({c: 0 for c in by.columns})
o["late"] = (o.get("late_refund", 0) < 0) | (o.get("undelivered_penalty", 0) < 0)
o["fee"] = -o["driver_fee"]
o["margin"] = o[[c for c in by.columns]].sum(axis=1)

leg0 = legs[legs.leg == 0].set_index("requestId")
o["pick"] = leg0["pickup.placeId"]
o["dest"] = leg0["delivery.placeId"]
o["ready_h"] = (leg0["pickup.window.from"] % 1440) // 60
o["day"] = leg0["pickup.window.from"] // 1440
o["private"] = leg0["pickup.privateAddress"] | leg0["delivery.privateAddress"]
rd = roads.set_index(["from", "to"])
o["min"] = [rd.loc[(a, b), "minutes"] for a, b in zip(o.pick, o.dest)]
home = drivers.homePlaceId.value_counts()
o["drv_pick"] = o.pick.map(home).fillna(0)
o["drv_drop"] = o.dest.map(home).fillna(0)
o["fee_ref"] = o.fee / o.referenceFeeCents
o["list_ref"] = o["list"] / o.referenceFeeCents

print(f"orders {len(o)}  late {o.late.mean():.1%}  margin €{o.margin.sum()/100:,.0f}")
print("\nlist / reference fee:", o.list_ref.describe().round(2).to_dict())
print("paid fee / reference fee (delivered):", o[o.fee > 0].fee_ref.describe().round(2).to_dict())


def table(col, bins=None):
    g = o.groupby(pd.cut(o[col], bins) if bins is not None else o[col], observed=True)
    t = g.agg(n=("late", "size"), late=("late", "mean"), margin=("margin", "mean"), fee_ref=("fee_ref", "mean"))
    t["margin"] = (t.margin / 100).round(1)
    print(f"\n-- by {col}\n" + t.round(2).to_string())


table("option")
table("day")
table("ready_h", [0, 9, 11, 13, 15, 17, 24])
table("min", [0, 60, 120, 180, 300, 600])
table("drv_pick", [-1, 5, 15, 40, 100, 1000])
table("drv_drop", [-1, 5, 15, 40, 100, 1000])
table("private")
print("\ndrivers by home town:\n" + home.to_string())
