"""Our dispatch system, built on the kit's starter.

On top of the starter it:
- declines requests it should not win: long drives, prices below the typical driver fee, tight
  same-day cars, and any new car while too many won cars still wait for a driver;
- quotes above the list price from level 4 (the competitor is 26-54% above it at level 8);
- posts one fee up to level 6, and a rising fee schedule at levels 7 and 8.
Every knob is in `Params`; `--param name=value` overrides one for a run.

Run it from the repo folder (it reads SPREAD_URL and SPREAD_KEY from .env):
  .venv\\Scripts\\python dispatch.py --level 8 [--seed TEXT] [--param name=value] [--board | --final]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import NamedTuple, Optional

from spread.client import Client, SpreadError, Unreachable, World
from spread.types import (
    Action, ActionResult, Audience, DeskEvent, DeskEventKind, DeskSnapshot, DriverProfile, FeeStep, Gig, Note, Order, PostAction,
    PriceRequest, Score, StopRef, TimeWindow,
)

HOUR, DAY = 60, 24 * 60
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# The events that need an answer. Views and account lines arrive with the next event that needs an answer.
WAKE_ON: list[DeskEventKind] = ["price_request", "order_won", "order_changed", "order_cancelled", "visit_failed", "gig_expired", "deadline_close",
                                "gig_dropped", "no_show", "driver_late"]


@dataclass
class Params:
    """The knobs of the strategy. `--param name=value` overrides one, so a sweep can compare settings."""
    # Quoting: the price of each option is its list price times the markup of its kind.
    # 1.45 beat 1.30 on all 8 level 8 test weeks (+29% margin); 1.60 won big in dear weeks and lost in cheap ones.
    markup_same_day: float = 1.45
    markup_next_day: float = 1.45
    markup_3_days: float = 1.45
    decline_factor: float = 20.0  # a decline is a quote this many times the list price: it never wins
    # Price learning (see Pricing), from level 4. Off: every quote is list price x markup. On 8 level 8 weeks,
    # learning + trust with a 1.3 floor lifted the worst week from 92.6% to 95.6% on time and the mean margin by ~6%.
    price_learn: bool = True
    price_up: float = 0.03  # a win raises the dealer's and the week's price level by this share
    price_down: float = 0.02  # a loss lowers them by this share (see Pricing for the win share this aims at)
    price_min: float = 1.30
    price_max: float = 1.90
    trust: bool = True  # level 8: +1% on a dealer's prices for each order delivered to it on time, up to 25
    # Limits tuned for level 8's driver shortage; the same at every level, since only level 8 counts.
    max_drive_min: int = 180  # decline when a leg's drive is longer than this (3-5 h drives: ~30% late at level 8)
    max_backlog: int = 10  # decline while this many won or likely-won orders have no driver
    min_price_fee_ratio: float = 1.0  # decline when our best price is below this many times the typical fee
    same_day_slack_min: int = 240  # decline tight same-day cars (see same_day_too_tight); 0 turns it off
    no_trade_in_max_level: int = 2  # up to this level, decline trade-ins: one never moved costs twice its price
    # Driver supply, learned during the week for each pickup town (see Supply). 0 turns a rule off.
    min_supply: float = 0.5  # decline when the town's chance that a gig finds a driver is below this
    supply_prior: float = 4.0  # how many gigs the first guess, from the drivers who live nearby, is worth
    supply_full_drivers: float = 30.0  # this many drivers within home_km make the first guess 0.9
    max_local_backlog: int = 0  # decline while this many cars within home_km of the pickup wait for a driver
    # Pairing: one driver takes two cars in a row when one car's delivery is near the other's pickup
    # (see Desk.find_partner). Allowed from level 3. pair_near_km = 0 turns it off.
    pair_near_km: float = 50.0  # car B's pickup within this many km (straight line) of car A's delivery
    pair_wait_min: int = 120  # a car that is not same-day may wait this long for its partner; a same-day car never waits
    pair_idle_min: int = 60  # the driver waits at most this long between delivering A and collecting B
    pair_buffer_min: int = 120  # both cars must still arrive this long before their deadlines
    pair_book_min: int = 60  # we expect a driver to start no sooner than this after posting
    pair_fee_share: float = 0.85  # the pair's typical fee is this share of the two cars' typical fees
    pair_timeout_min: int = 60  # a pair that no driver books in this time goes back to one gig per car
    # After a driver drops a gig or does not show (see Desk.after_drop) and when a driver will be late.
    # Off: on 8 level 8 weeks it lifted the worst week's on-time share by ~0.6 points but cost ~EUR 120 a week.
    drop_react: bool = False  # False keeps the starter's behaviour: the gig goes back with its old schedule
    drop_urgent_min: int = 360  # with less than this left to the last safe pickup, go straight to the steep fees
    drop_bump: float = 0.10  # otherwise restart the rising fees from the fee then shown plus this share of the typical fee
    late_release_min: int = 120  # release a late driver whose car would arrive late, if this long is left to find another
    # Up to this level, a gig gets one fee, and one small step after the deadline warning: up to level 6
    # the typical fee books most gigs, and a single fee does not teach drivers to wait for raises.
    flat_fee_max_level: int = 6
    flat_fee_urgent: float = 1.15
    # Short trips cost the most per km (a fixed part for the driver's trips, then about 0.26 EUR/km), and a
    # driver who lives near one needs less. A car whose drive is at most `short_km` opens at
    # `short_fee_open` times the typical fee instead of `fee_open`. 0 turns it off.
    short_km: float = 0.0
    short_fee_open: float = 1.0
    min_home_drivers: int = 0  # decline when fewer drivers live within `home_km` of the pickup
    home_km: float = 40.0
    # Driver fees, as multiples of the typical fee.
    fee_open: float = 1.0
    fee_rise: float = 0.10  # added each `fee_every` minutes
    fee_every: int = 60
    fee_cap: float = 1.5
    steep_open: float = 1.5
    steep_rise: float = 0.25  # added each half hour after the deadline warning
    steep_cap: float = 2.5


def parse_params(pairs: list[str]) -> Params:
    params = Params()
    for pair in pairs:
        name, _, value = pair.partition("=")
        if not hasattr(params, name):
            raise SystemExit(f"Unknown parameter {name!r}. Known: {', '.join(vars(params))}")
        kind = type(getattr(params, name))
        if kind is bool:  # bool("False") is True, so read the word
            if value.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise SystemExit(f"Parameter {name} takes true or false, not {value!r}")
            setattr(params, name, value.lower() in ("true", "1", "yes"))
        else:
            setattr(params, name, kind(value))
    return params


class Geo:
    """The map's road minutes and where the drivers live."""

    def __init__(self, world_map: dict, drivers: list[DriverProfile]):
        self.places = {p["id"]: p for p in world_map["places"]}
        self.minutes = {(r["from"], r["to"]): r["minutes"] for r in world_map["roads"]}
        self.km_road = {(r["from"], r["to"]): r["km"] for r in world_map["roads"]}
        self.homes: dict[str, int] = {}
        for driver in drivers:
            self.homes[driver["homePlaceId"]] = self.homes.get(driver["homePlaceId"], 0) + 1

    def km(self, a: str, b: str) -> float:
        pa, pb = self.places[a], self.places[b]
        dlat, dlon = math.radians(pb["lat"] - pa["lat"]), math.radians(pb["lon"] - pa["lon"])
        h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(pa["lat"])) * math.cos(math.radians(pb["lat"])) * math.sin(dlon / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(h))

    def drivers_near(self, place: str, km: float) -> int:
        return sum(n for home, n in self.homes.items() if self.km(place, home) <= km)


class Supply:
    """How reliably our gigs from each pickup town find a driver, learned as the week goes on.

    Each town starts from a guess: the more drivers live within `home_km`, the likelier a booking.
    The guess counts as `supply_prior` gigs. A booking counts as a success; a deadline warning or a
    gig that expired unbooked counts as a failure. The estimate is (successes + prior hits) /
    (gigs + prior), so a town with no gigs yet keeps its guess, and a town whose gigs keep failing
    drops fast. At level 8 each town keeps a different share of its drivers, new in every world,
    so this is the part of the shortage that only the week itself shows."""

    def __init__(self, geo: Geo, params: Params):
        self.geo, self.params = geo, params
        self.booked: dict[str, float] = {}
        self.failed: dict[str, float] = {}
        self.guess: dict[str, float] = {}

    def prior(self, place: str) -> float:
        if place not in self.guess:
            near = self.geo.drivers_near(place, self.params.home_km)
            self.guess[place] = min(0.9, max(0.2, 0.9 * near / self.params.supply_full_drivers))
        return self.guess[place]

    def success(self, place: str) -> None:
        self.booked[place] = self.booked.get(place, 0) + 1

    def failure(self, place: str) -> None:
        self.failed[place] = self.failed.get(place, 0) + 1

    def chance(self, place: str) -> float:
        k = self.params.supply_prior
        b, f = self.booked.get(place, 0), self.failed.get(place, 0)
        return (b + k * self.prior(place)) / (b + f + k)


class Pricing:
    """The price level that each dealer accepts from us, learned from our own wins and losses.

    The competitor's price is the list price times a level for the week (26-54% above list at level 8),
    a factor for the lane, one for the day, and noise, and it answers our wins. We never see it, only
    whether each quote won. So each dealer gets a multiplier of the list price: a win raises it by
    `price_up`, a loss lowers it by `price_down`. In the long run that wins about
    price_down / (price_up + price_down) of the quotes we want, at the highest price that does. A dealer we
    have not quoted yet starts from the week's level, which learns from every dealer the same way.
    Only quotes meant to win count: a decline at 20x the list price says nothing.

    Dealer trust (level 8): each dealer accepts 1% more from us for each order delivered to it on time,
    up to 25; a late order halves its count."""

    def __init__(self, params: Params):
        self.params = params
        start = params.markup_same_day
        self.week = start
        self.dealer: dict[str, float] = {}
        self.on_time: dict[str, int] = {}

    def level(self, dealer: str) -> float:
        return self.dealer.get(dealer, self.week)

    def trust_factor(self, dealer: str) -> float:
        return 1 + 0.01 * self.on_time.get(dealer, 0) if self.params.trust else 1.0

    def multiplier(self, dealer: str) -> float:
        return self.level(dealer) * self.trust_factor(dealer)

    def learn(self, dealer: str, won: bool) -> None:
        p = self.params
        step = (1 + p.price_up) if won else (1 - p.price_down)
        clamp = lambda x: min(p.price_max, max(p.price_min, x))
        self.dealer[dealer] = clamp(self.level(dealer) * step)
        self.week = clamp(self.week * step)

    def delivered(self, dealer: str, on_time: bool) -> None:
        count = self.on_time.get(dealer, 0)
        self.on_time[dealer] = min(25, count + 1) if on_time else count // 2


class Retry(NamedTuple):
    """How to post a leg again after a failed visit."""
    pickup_from: int = 0
    manual_licence: bool = False
    plates: bool = False


class Desk:
    """What the starter remembers, and its answer to each event. The answers wait in the outbox
    until the starter sends them, and the world applies them at the current minute."""

    def __init__(self, world: World, level: int, now: int, params: Optional[Params] = None, geo: Optional[Geo] = None):
        self.world, self.level, self.now = world, level, now
        self.params = params or Params()
        self.geo = geo or Geo(world.client.map(), world.roster())
        self.pending: set[str] = set()  # requests quoted to win, not yet decided
        self.declined = 0
        self.supply = Supply(self.geo, self.params)
        self.pricing = Pricing(self.params)
        self.gig_place: dict[str, str] = {}  # gig id -> its first pickup place
        self.pair_gigs: dict[str, int] = {}  # open two-car gig id -> the minute it was posted
        self.pair_posts: set[int] = set()  # id() of the pair posts in the outbox
        self.paired = 0  # pairs posted this week
        self.refused_quotes = 0  # quotes the world refused, mostly for coming too late
        self.outbox: list[Action] = []  # the actions decided since the starter last sent
        self.requests: dict[str, PriceRequest] = {}
        self.orders: dict[str, Order] = {}  # the orders won and not cancelled
        self.leg_gigs: dict[str, dict[int, str]] = {}  # order id -> leg -> the gig that carries the leg
        self.booked: set[str] = set()  # the gigs that a driver holds now
        self.posts: dict[str, PostAction] = {}  # gig id -> the post that made it
        self.urgent: set[str] = set()  # the orders whose deadline warning has come

    def rejoin(self, snapshot: DeskSnapshot, history: list[DeskEvent]) -> None:
        """Rebuilds what it knows from the world, so a starter that stopped keeps its orders. Then answers
        what it missed: open requests, orders without a gig, failed visits without a new gig, and the
        deadline warnings, so the orders that had one keep their steep fees."""
        posted: dict[str, list[Gig]] = {}  # order id -> its gigs, oldest first
        self.booked = {gig["id"] for gig in snapshot["gigs"] if gig["status"] in ("booked", "working", "done")}
        for gig in snapshot["gigs"]:
            posted.setdefault(gig["stops"][0]["orderId"], []).append(gig)
            if gig["status"] in ("open", "booked", "working"):
                self.carry({"type": "post", "stops": gig["stops"], "audience": gig["audience"], "schedule": gig["schedule"]}, gig["id"])
        self.requests = {e["request"]["id"]: e["request"] for e in history if e["kind"] == "price_request"}
        self.urgent = {e["orderId"] for e in history if e["kind"] == "deadline_close"}
        self.orders = {o["id"]: o for o in snapshot["orders"] if o["id"] in self.requests and o["status"] != "cancelled"}
        for order in self.orders.values():
            if order["status"] == "waiting" and not self.gigs_of(order["id"]):
                self.post(order, undelivered(order))
        for event in history:
            order = self.orders.get(event.get("orderId", ""))
            if order is None:
                continue
            if event["kind"] == "visit_failed" and order["deliveredAt"][event["leg"]] is None:
                later = [g for g in posted.get(order["id"], []) if g["postedAt"] >= event["at"] and g["id"] != event["gigId"]]
                if not any(stop["leg"] == event["leg"] for gig in later for stop in gig["stops"]):
                    self.retry(order["id"], event["leg"], event["reason"])
        # What the week has shown about driver supply, from the same history.
        for gig in snapshot["gigs"]:
            self.gig_place[gig["id"]] = self.pickup_place(gig["stops"][0]["orderId"], gig["stops"][0]["leg"])
            if gig["status"] == "open" and len({s["orderId"] for s in gig["stops"]}) > 1:
                self.pair_gigs[gig["id"]] = gig["postedAt"]
        for event in history:
            self.learn(event)
        for request in snapshot["openRequests"]:
            self.quote(request)

    def save_state(self, path: str) -> None:
        """Writes what the world's history cannot give back: the learned prices, dealer trust and the
        quotes that still wait for an answer. A failed write is not worth stopping for."""
        state = {"week": self.pricing.week, "dealer": self.pricing.dealer, "on_time": self.pricing.on_time,
                 "pending": sorted(self.pending)}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(path + ".tmp", path)
        except OSError as error:
            print(f"Could not save the desk's state: {error}", file=sys.stderr, flush=True)

    def load_state(self, path: str) -> None:
        """Reads what save_state wrote for this world, when a restarted system finds it."""
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return
        self.pricing.week = state.get("week", self.pricing.week)
        self.pricing.dealer.update(state.get("dealer", {}))
        self.pricing.on_time.update(state.get("on_time", {}))
        # Only quotes still waiting: the answers to the others are in the history already.
        self.pending |= {r for r in state.get("pending", []) if r in self.requests and self.requests[r]["respondBy"] > self.now}
        print(f"Picked up the learned prices of {len(self.pricing.dealer)} dealers from {path}.", flush=True)

    def pickup_place(self, order_id: str, leg: int = 0) -> str:
        source = self.orders.get(order_id) or self.requests.get(order_id)
        return source["legs"][leg]["pickup"]["placeId"] if source else ""

    def learn(self, event: DeskEvent) -> None:
        """Feeds driver supply: a booking is a success for its pickup town, a deadline warning or an
        expired gig a failure."""
        if event["kind"] == "gig_booked":
            place = self.gig_place.get(event["gigId"])
            if place:
                self.supply.success(place)
        elif event["kind"] == "gig_expired":
            place = self.gig_place.get(event["gigId"])
            if place:
                self.supply.failure(place)
        elif event["kind"] == "deadline_close":
            place = self.pickup_place(event["orderId"])
            if place:
                self.supply.failure(place)

    def handle(self, event: DeskEvent) -> None:
        self.learn(event)
        if event["kind"] == "price_request":
            self.quote(event["request"])
        elif event["kind"] == "order_won":
            if event["order"]["id"] in self.pending:
                self.pricing.learn(event["order"]["dealerId"], True)
            self.pending.discard(event["order"]["id"])
            self.win(event["order"])
        elif event["kind"] == "order_lost":
            if event["requestId"] in self.pending and event["requestId"] in self.requests:
                self.pricing.learn(self.requests[event["requestId"]]["dealerId"], False)
            self.pending.discard(event["requestId"])
        elif event["kind"] == "order_changed":
            self.change(event["order"])
        elif event["kind"] == "order_cancelled":  # the world has stopped the car and told each of its gigs what happens
            for books in (self.orders, self.leg_gigs):
                books.pop(event["orderId"], None)
            # A withdrawn two-car gig also took the partner's car out of the app: post the partner alone.
            for gig in event["gigs"]:
                if gig["outcome"] == "withdrawn" and self.carries_two(gig["gigId"]):
                    self.split(gig["gigId"], withdraw=False)
        elif event["kind"] == "visit_failed":
            self.retry(event["orderId"], event["leg"], event["reason"])
        elif event["kind"] == "gig_expired":
            if self.carries_two(event["gigId"]):
                self.split(event["gigId"], withdraw=False)
            else:
                self.repost(event["gigId"])
        elif event["kind"] == "deadline_close":  # it comes again when a driver drops the car after the first warning
            self.urgent.add(event["orderId"])
            for gig_id in self.gigs_of(event["orderId"]):
                if gig_id not in self.booked and self.carries_two(gig_id):
                    self.split(gig_id)  # each car goes alone, this one at the steep fees
            self.raise_fees(event["orderId"])
        elif event["kind"] == "gig_booked":
            self.booked.add(event["gigId"])
        elif event["kind"] in ("gig_dropped", "no_show"):  # after a no-show the gig is open again; a late driver still comes
            self.booked.discard(event["gigId"])
            if self.params.drop_react:
                self.after_drop(event["gigId"])
        elif event["kind"] == "driver_late":
            if self.params.drop_react:
                self.after_late(event["gigId"], event["pickupAt"])
        elif event["kind"] == "delivered":
            self.delivered(event["orderId"], event["leg"], event["at"])

    def delivered(self, order_id: str, leg: int, at: int) -> None:
        self.leg_gigs.get(order_id, {}).pop(leg, None)
        if order_id in self.orders:
            order = self.orders[order_id]
            order["deliveredAt"][leg] = at
            if not undelivered(order):  # the last car: the order counts for the dealer's trust
                self.pricing.delivered(order["dealerId"], max(order["deliveredAt"]) <= order["deadline"])

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        reason = self.decline_reason(request)
        if reason:
            self.declined += 1
            if self.level > 3:  # up to level 3 every quote wins, so a decline sends no quote
                prices = {o["kind"]: int(o["listPriceCents"] * self.params.decline_factor) for o in request["options"]}
                self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})
            return
        if self.level <= 3:
            first = request["options"][0]
            prices = {first["kind"]: first["listPriceCents"]}
        else:
            markup = self.markups(request)
            prices = {o["kind"]: int(o["listPriceCents"] * markup[o["kind"]] + 0.5) for o in request["options"]}
        self.pending.add(request["id"])
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def decline_reason(self, request: PriceRequest) -> Optional[str]:
        """Why not to take this request, or None to quote it."""
        p = self.params
        if self.level <= p.no_trade_in_max_level and len(request["legs"]) > 1:
            return "trade-in"
        for leg in request["legs"]:
            a, b = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
            if self.geo.minutes.get((a, b), 0) > p.max_drive_min:
                return "long drive"
            if p.min_home_drivers and self.geo.drivers_near(a, p.home_km) < p.min_home_drivers:
                return "few drivers"
        if self.best_price(request) < request["referenceFeeCents"] * p.min_price_fee_ratio:
            return "money-losing"
        if self.same_day_too_tight(request):
            return "same-day too tight"
        place = request["legs"][0]["pickup"]["placeId"]
        if p.min_supply and self.supply.chance(place) < p.min_supply:
            return "few drivers book there"
        if p.max_local_backlog and self.local_backlog(place) >= p.max_local_backlog:
            return "local backlog"
        if self.backlog() >= p.max_backlog:
            return "backlog"
        return None

    def same_day_too_tight(self, request: PriceRequest) -> bool:
        """True when the first option is same-day and the drive leaves less than `same_day_slack_min`
        minutes between the request and the deadline. Such cars arrive late often enough to give the
        price back. (JonBacc's rule; at level 4 and up it applies only when same-day is the only option.)"""
        first, slack = request["options"][0], self.params.same_day_slack_min
        if not slack or first["kind"] != "same_day" or (self.level > 3 and len(request["options"]) > 1):
            return False
        drive = sum(self.geo.minutes.get((leg["pickup"]["placeId"], leg["delivery"]["placeId"]), 0) for leg in request["legs"])
        return first["deadline"] - request["arrivedAt"] - drive < slack

    def best_price(self, request: PriceRequest) -> float:
        """The most that this request can pay us: up to level 3 the first option's list price, which every
        quote wins; from level 4 the dearest option at our markup."""
        if self.level <= 3:
            return request["options"][0]["listPriceCents"]
        markup = self.markups(request)
        return max(o["listPriceCents"] * markup[o["kind"]] for o in request["options"])

    def markups(self, request: PriceRequest) -> dict[str, float]:
        """The multiple of the list price for each option: learned per dealer, or the fixed markups."""
        p = self.params
        if p.price_learn or p.trust:
            m = self.pricing.multiplier(request["dealerId"]) if p.price_learn else p.markup_same_day * self.pricing.trust_factor(request["dealerId"])
            return {"same_day": m, "next_day": m, "within_3_days": m}
        return {"same_day": p.markup_same_day, "next_day": p.markup_next_day, "within_3_days": p.markup_3_days}

    def local_backlog(self, place: str) -> int:
        """The cars that wait for a driver, or may still be won, with a pickup within home_km of `place`."""
        near = lambda other: other == place or self.geo.km(place, other) <= self.params.home_km
        count = sum(1 for order_id in self.waiting_orders() if near(self.pickup_place(order_id)))
        return count + sum(1 for r in self.pending if r in self.requests and near(self.requests[r]["legs"][0]["pickup"]["placeId"]))

    def waiting_orders(self) -> list[str]:
        """The won orders with an undelivered car that no driver holds."""
        waiting = []
        for order_id, order in self.orders.items():
            if not undelivered(order):
                continue
            gigs = self.gigs_of(order_id)
            if not gigs or any(g not in self.booked for g in gigs):
                waiting.append(order_id)
        return waiting

    def backlog(self) -> int:
        """The orders that wait for a driver, and the quotes that may still win."""
        return len(self.waiting_orders()) + len(self.pending)

    def win(self, order: Order) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:  # rejoin can know it already
            self.orders[order["id"]] = order
            if not self.try_pair(order):
                self.post(order, undelivered(order))

    # ---- Pairing: one driver, two cars in a row ----

    def pairable(self, order: Order) -> bool:
        """A one-car order at dealer sites, not yet warned, with its car still to move."""
        leg = order["legs"][0]
        return (len(order["legs"]) == 1 and undelivered(order) == [0] and order["id"] not in self.urgent
                and not leg["pickup"]["privateAddress"] and not leg["delivery"]["privateAddress"])

    def try_pair(self, order: Order) -> bool:
        """Posts the new order in one gig with a waiting order that fits before or after it. False when none fits."""
        p = self.params
        if not p.pair_near_km or self.level < 3 or not self.pairable(order):
            return False
        best = None
        for other in list(self.orders.values()):
            if other["id"] == order["id"] or not self.pairable(other) or not self.free_to_pair(other["id"]):
                continue
            for first, second in ((order, other), (other, order)):
                hop = self.pair_fits(first, second)
                if hop is not None and (best is None or hop < best[0]):
                    best = (hop, first, second, other)
        if best is None:
            return False
        _, first, second, other = best
        if not self.take_back(other["id"]):
            return False
        self.post_pair(first, second)
        return True

    def free_to_pair(self, order_id: str) -> bool:
        """The order's car waits alone: in one open gig that no driver holds, or in a post still in the outbox."""
        if any(a["type"] == "post" and {s["orderId"] for s in a["stops"]} == {order_id} for a in self.outbox):
            return True
        gigs = self.gigs_of(order_id)
        return len(gigs) == 1 and gigs[0] not in self.booked and gigs[0] in self.posts and not self.carries_two(gigs[0])

    def pair_fits(self, a: Order, b: Order) -> Optional[int]:
        """The road minutes from A's delivery to B's pickup when one driver can take A, then B, with both on
        time; None when not. A same-day car never waits for its partner; another waits at most pair_wait_min."""
        p, la, lb = self.params, a["legs"][0], b["legs"][0]
        pa, qa, pb, qb = la["pickup"]["placeId"], la["delivery"]["placeId"], lb["pickup"]["placeId"], lb["delivery"]["placeId"]
        if self.geo.km(qa, pb) > p.pair_near_km:
            return None
        drive_a, hop, drive_b = self.geo.minutes[(pa, qa)], self.geo.minutes[(qa, pb)], self.geo.minutes[(pb, qb)]
        if drive_a + hop + drive_b > 12 * HOUR:
            return None
        soonest = self.now + p.pair_book_min
        start = max(soonest, la["pickup"]["window"]["from"])
        at_b = start + drive_a + hop
        ready_b = lb["pickup"]["window"]["from"]
        collect = max(at_b, ready_b)
        if collect - at_b > p.pair_idle_min:
            return None
        if collect - max(soonest, ready_b) > (0 if b["option"] == "same_day" else p.pair_wait_min):
            return None
        if start + drive_a + p.pair_buffer_min > a["deadline"] or collect + drive_b + p.pair_buffer_min > b["deadline"]:
            return None
        if collect > b["lastPickupAt"] - p.pair_buffer_min:
            return None
        return hop

    def take_back(self, order_id: str) -> bool:
        """Takes the order's lone gig out of the app (or its post out of the outbox). False when a driver got there first."""
        for action in self.outbox:
            if action["type"] == "post" and {s["orderId"] for s in action["stops"]} == {order_id}:
                self.outbox.remove(action)
                return True
        gig_id = self.gigs_of(order_id)[0]
        result = self.world.act([{"type": "withdraw", "gigId": gig_id}])[0]
        if result["ok"] is not True:
            return False
        self.leg_gigs.pop(order_id, None)
        return True

    def post_pair(self, a: Order, b: Order) -> None:
        """One gig: A's pickup and delivery, then B's. Its fee is a share of the two typical fees."""
        p = self.params
        stops = stops_for(a, [0], self.requests[a["id"]]["note"], Retry()) + stops_for(b, [0], self.requests[b["id"]]["note"], Retry())
        audience: Audience = {"experienced": any(o["legs"][0]["car"]["premium"] for o in (a, b)), "manualLicence": False}
        fee = (self.requests[a["id"]]["referenceFeeCents"] + self.requests[b["id"]]["referenceFeeCents"]) * p.pair_fee_share
        if self.level <= p.flat_fee_max_level:
            fees: list[FeeStep] = [{"at": self.now, "feeCents": max(1, int(fee * p.fee_open + 0.5))}]
        else:
            fees = schedule(self.now, fee, self.level, p.fee_open, p.fee_rise, p.fee_every, p.fee_cap)
        action: PostAction = {"type": "post", "stops": without_windows(stops, self.now), "audience": audience, "schedule": fees}
        self.outbox.append(action)
        self.pair_posts.add(id(action))
        self.paired += 1

    def carries_two(self, gig_id: str) -> bool:
        action = self.posts.get(gig_id)
        return action is not None and len({s["orderId"] for s in action["stops"]}) > 1

    def split(self, gig_id: str, withdraw: bool = True) -> None:
        """Ends a two-car gig that no driver holds and posts each of its cars alone."""
        action = self.posts.get(gig_id)
        self.pair_gigs.pop(gig_id, None)
        if action is None or gig_id in self.booked:
            return
        if withdraw:
            self.outbox.append({"type": "withdraw", "gigId": gig_id})
        for order_id in dict.fromkeys(s["orderId"] for s in action["stops"]):
            legs = self.leg_gigs.get(order_id, {})
            for leg in [leg for leg, g in legs.items() if g == gig_id]:
                legs.pop(leg)
            order = self.orders.get(order_id)
            if order is not None and undelivered(order) and not self.gigs_of(order_id):
                self.post(order, undelivered(order))

    # ---- Drops, no-shows and late drivers ----

    def after_drop(self, gig_id: str) -> None:
        """The gig is open again. A driver who gave it back takes it again only for more, and its old schedule
        may already be at its top. Close to the last safe pickup: the steep fees at once. Otherwise: a fresh
        rising schedule from the fee then shown plus a bump. A pair goes back to one gig per car."""
        action = self.posts.get(gig_id)
        if action is None or gig_id not in self.gigs_of(action["stops"][0]["orderId"]):
            return
        if self.carries_two(gig_id):
            self.split(gig_id)
            return
        order = self.orders.get(action["stops"][0]["orderId"])
        if order is None or not undelivered(order):
            return
        p, cars = self.params, pickups(action)
        if order["lastPickupAt"] - self.now < p.drop_urgent_min:
            self.urgent.add(order["id"])
            fees = self.fees_for(order, cars)
        else:
            base = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
            shown = max([s["feeCents"] for s in action["schedule"] if s["at"] <= self.now] or [action["schedule"][0]["feeCents"]])
            start = shown / base + p.drop_bump
            if self.level <= p.flat_fee_max_level:
                fees = [{"at": self.now, "feeCents": max(1, int(base * start + 0.5))}]
            else:
                fees = schedule(self.now, base, self.level, start, p.fee_rise, p.fee_every, max(p.fee_cap, start))
        self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": fees})
        self.posts[gig_id] = {**action, "schedule": fees}

    def after_late(self, gig_id: str, pickup_at: int) -> None:
        """A booked driver will collect the first car late. Keep them unless the cars would then arrive late
        and there is still time to find another driver: then release them (free after driver_late) and post
        the cars again at the steep fees."""
        action = self.posts.get(gig_id)
        if action is None or gig_id not in self.booked:
            return
        order_ids = list(dict.fromkeys(s["orderId"] for s in action["stops"]))
        orders = [self.orders[o] for o in order_ids if o in self.orders]
        if not orders:
            return
        # When the driver's cars would arrive, by the road minutes from the late pickup.
        at, place, late = pickup_at, None, False
        for stop in action["stops"]:
            order = self.orders.get(stop["orderId"])
            if order is None:
                continue
            leg = order["legs"][stop["leg"]]
            here = leg["pickup"]["placeId"] if stop["kind"] == "pickup" else leg["delivery"]["placeId"]
            if place is not None:
                at += self.geo.minutes.get((place, here), 0)
            place = here
            if stop["kind"] == "delivery" and at > order["deadline"]:
                late = True
        if not late or min(o["lastPickupAt"] for o in orders) - self.now < self.params.late_release_min:
            return
        self.outbox.append({"type": "release", "gigId": gig_id})
        self.booked.discard(gig_id)
        self.pair_gigs.pop(gig_id, None)
        for order in orders:
            self.urgent.add(order["id"])
            legs = self.leg_gigs.get(order["id"], {})
            for leg in [leg for leg, g in legs.items() if g == gig_id]:
                legs.pop(leg)
            if undelivered(order) and not self.gigs_of(order["id"]):
                self.post(order, undelivered(order))

    def tick(self) -> None:
        """A pair that no driver has booked within pair_timeout_min goes back to one gig per car."""
        for gig_id, posted in list(self.pair_gigs.items()):
            if gig_id in self.booked:
                del self.pair_gigs[gig_id]
            elif self.now - posted >= self.params.pair_timeout_min:
                self.split(gig_id)

    def next_tick(self) -> Optional[int]:
        """The next minute at which tick has work, or None."""
        due = [posted + self.params.pair_timeout_min for g, posted in self.pair_gigs.items() if g not in self.booked]
        return max(self.now + 1, min(due)) if due else None

    def change(self, order: Order) -> None:
        if order["id"] in self.orders:
            self.orders[order["id"]] = order
            if self.withdraw(order["id"]):
                self.post(order, undelivered(order))

    def repost(self, gig_id: str) -> None:
        """Posts a gig again when one of its windows ended before a driver booked it."""
        action = self.posts.get(gig_id)
        # A gig the starter has already taken off and posted again, after a change of plan, needs nothing.
        if action is None or not any(self.leg_gigs.get(s["orderId"], {}).get(s["leg"]) == gig_id for s in action["stops"]):
            return
        for stop in action["stops"]:
            self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
        order = self.orders.get(action["stops"][0]["orderId"])
        if order is None:
            return
        # One of its windows has ended, perhaps at a site's closing time before the window's own end: post without its own stop windows.
        stops = without_windows(action["stops"], float("inf"))
        self.outbox.append({**action, "stops": stops, "schedule": self.fees_for(order, pickups(action))})

    def raise_fees(self, order_id: str) -> None:
        """From the order's deadline warning on, its gigs that no driver holds go at the steep fees."""
        order = self.orders.get(order_id)
        if order is None:
            return
        self.urgent.add(order_id)
        self.send_post_of(order_id)
        for gig_id in self.gigs_of(order_id):
            if gig_id not in self.booked:
                self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": self.fees_for(order, pickups(self.posts[gig_id]))})

    def fees_for(self, order: Order, cars: int) -> list[FeeStep]:
        """The fees for some of the order's cars from now: rising, or steep once its deadline warning has come."""
        fee = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        p = self.params
        opening = p.short_fee_open if p.short_km and self.longest_km(order) <= p.short_km else p.fee_open
        if self.level <= p.flat_fee_max_level:
            factor = p.flat_fee_urgent if order["id"] in self.urgent else opening
            return [{"at": self.now, "feeCents": max(1, int(fee * factor + 0.5))}]
        if order["id"] in self.urgent:
            return schedule(self.now, fee, self.level, p.steep_open, p.steep_rise, 30, p.steep_cap)
        return schedule(self.now, fee, self.level, opening, p.fee_rise, p.fee_every, max(p.fee_cap, opening))

    def longest_km(self, order: Order) -> float:
        """The road km of the order's longest leg."""
        return max(self.geo.km_road.get((leg["pickup"]["placeId"], leg["delivery"]["placeId"]), 0) for leg in order["legs"])

    def retry(self, order_id: str, leg: int, reason: str) -> None:
        if order_id in self.orders:
            order = self.orders[order_id]
            self.leg_gigs.get(order_id, {}).pop(leg, None)  # the world has taken the car out of the failed gig
            car_not_there = reason in ("not_ready", "closed")
            pickup_from = retry_from(order, self.requests[order_id], leg, self.now) if car_not_there else 0
            self.post(order, [leg], Retry(pickup_from, manual_licence=reason == "licence", plates=reason == "no_plates"))

    def post(self, order: Order, legs: list[int], retry: Retry = Retry()) -> None:
        # A premium car needs an experienced driver: the fine is €150 a car.
        audience: Audience = {"experienced": any(leg["car"]["premium"] for leg in order["legs"]), "manualLicence": retry.manual_licence}
        stops = without_windows(stops_for(order, legs, self.requests[order["id"]]["note"], retry), self.now)
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": self.fees_for(order, len(legs))})
        if len(legs) == 1:
            return
        # A trade-in over the 12-hour limit for a gig with two cars goes as one gig per car.
        self.send()
        if not self.gigs_of(order["id"]):
            for leg in legs:
                self.post(order, [leg], retry)

    def gigs_of(self, order_id: str) -> list[str]:
        """The gigs that carry the order's undelivered legs: one, or one for each leg of a split trade-in."""
        return list(dict.fromkeys(self.leg_gigs.get(order_id, {}).values()))

    def carry(self, action: PostAction, gig_id: str) -> None:
        """Remembers a gig that the world accepted: the post that made it, and the legs it carries."""
        for stop in action["stops"]:
            self.leg_gigs.setdefault(stop["orderId"], {})[stop["leg"]] = gig_id
        self.posts[gig_id] = action
        first = action["stops"][0]
        self.gig_place[gig_id] = self.pickup_place(first["orderId"], first["leg"])

    def withdraw(self, order_id: str) -> bool:
        """Takes an order's gigs out of the drivers' app. False, and nothing withdrawn, when a driver holds one of them."""
        self.send_post_of(order_id)
        gigs = self.gigs_of(order_id)
        if any(gig_id in self.booked for gig_id in gigs):
            return False
        self.leg_gigs.pop(order_id, None)
        self.outbox += [{"type": "withdraw", "gigId": gig_id} for gig_id in gigs]
        # A two-car gig also carried a partner's car: the partner goes alone now.
        for gig_id in gigs:
            if self.carries_two(gig_id):
                self.pair_gigs.pop(gig_id, None)
                for partner_id in {s["orderId"] for s in self.posts[gig_id]["stops"]} - {order_id}:
                    self.leg_gigs.pop(partner_id, None)
                    partner = self.orders.get(partner_id)
                    if partner is not None and undelivered(partner):
                        self.post(partner, undelivered(partner))
        return True

    def send_post_of(self, order_id: str) -> None:
        """A post of the order still in the outbox goes to the world now, so the starter knows its gig. Each gig carries one order."""
        if any(action["type"] == "post" and action["stops"][0]["orderId"] == order_id for action in self.outbox):
            self.send()

    def send(self) -> None:
        """Sends the outbox now. In practice, the outbox goes with the next advance instead."""
        actions, self.outbox = self.outbox, []
        if actions:
            self.read_results(actions, self.world.act(actions))

    def read_results(self, actions: list[Action], results: list[ActionResult]) -> None:
        """Remembers the gig of each post that the world accepted."""
        for action, result in zip(actions, results):
            if action["type"] == "quote" and result["ok"] is not True:
                self.refused_quotes += 1  # in the final, mostly a quote that came after the request's 15 minutes
                if self.refused_quotes <= 3:
                    print(f"Quote refused: {result.get('reason')}", file=sys.stderr, flush=True)
            pair = id(action) in self.pair_posts
            self.pair_posts.discard(id(action))
            if action["type"] == "post" and result["ok"] is True:
                self.carry(action, result["id"])
                if pair:
                    self.pair_gigs[result["id"]] = self.now
            elif pair:  # the world refused the pair: each car goes alone
                for order_id in dict.fromkeys(s["orderId"] for s in action["stops"]):
                    order = self.orders.get(order_id)
                    if order is not None and undelivered(order) and not self.gigs_of(order_id):
                        self.post(order, undelivered(order))


def retry_from(order: Order, request: PriceRequest, leg: int, now: int) -> int:
    """When to try a car again after a visit found it not ready or the site closed: two hours on, or,
    when the dealer moved the deadline for a car that is late, when the car should be ready by the
    same delay."""
    promised = next((o["deadline"] for o in request["options"] if o["kind"] == order["option"]), order["deadline"])
    delay = max(0, order["deadline"] - promised)
    return max(now + 2 * HOUR, order["legs"][leg]["pickup"]["window"]["from"] + delay)


def undelivered(order: Order) -> list[int]:
    return [leg for leg in range(len(order["legs"])) if order["deliveredAt"][leg] is None]


def pickups(action: PostAction) -> int:
    """The cars a post carries."""
    return sum(1 for stop in action["stops"] if stop["kind"] == "pickup")


def without_windows(stops: list[StopRef], before: float) -> list[StopRef]:
    """Leaves out the stop windows that end before `before`, so the world's window applies: once the order's
    window has ended, it runs to the end of the world."""
    return [{k: v for k, v in stop.items() if k != "window"} if "window" in stop and stop["window"]["to"] < before else stop for stop in stops]


def schedule(now: int, fee: float, level: int, start: float, rise: float, every: int, cap: float) -> list[FeeStep]:
    """`start` times the fee at once, then `rise` times it more every `every` minutes, up to `cap` times.
    Level 1 takes one fee only."""
    steps = 1 if level == 1 or rise <= 0 else min(48, int(round((cap - start) / rise)) + 1)
    return [{"at": now + k * every, "feeCents": max(1, int(fee * min(cap, start + k * rise) + 0.5))} for k in range(max(1, steps))]


def stops_for(order: Order, legs: list[int], note: Optional[Note], retry: Retry) -> list[StopRef]:
    """A pickup and a delivery for each leg. An electric car under 30% gets a charging stop.
    A stop has its own window only when the note or the retry makes it narrower. Without
    one, the world plans the pickup in the order's window and the delivery by the deadline."""
    stops: list[StopRef] = []
    for leg in legs:
        car, pickup, delivery = order["legs"][leg]["car"], order["legs"][leg]["pickup"], order["legs"][leg]["delivery"]
        noted = window_from_note(note, pickup["window"], "pickup")
        start = max(noted["from"], retry.pickup_from)
        pickup_window: TimeWindow = {"from": start, "to": max(noted["to"], start)}
        delivery_base: TimeWindow = {"from": delivery["window"]["from"], "to": max(delivery["window"]["from"], order["deadline"])}
        delivery_window = window_from_note(note, delivery_base, "delivery")
        low_battery = car["electric"] and car["chargePercent"] is not None and car["chargePercent"] < 30
        first: StopRef = {"orderId": order["id"], "leg": leg, "kind": "pickup", "withPlates": retry.plates, "withCharging": low_battery}
        second: StopRef = {"orderId": order["id"], "leg": leg, "kind": "delivery"}
        stops += [narrower(first, pickup_window, pickup["window"]), narrower(second, delivery_window, delivery_base)]
    return stops


def narrower(stop: StopRef, window: TimeWindow, base: TimeWindow) -> StopRef:
    return {**stop, "window": window} if window["from"] > base["from"] or window["to"] < base["to"] else stop


# "Not before", "at the earliest" and a ready time all mean from then on; read them before BEFORE.
# re.ASCII makes \b and \d mean what they mean in JavaScript, so both starters read a note alike.
AFTER = re.compile(r"jälkeen|alkaen|aikaisintaan|\bafter\b|\bfrom\b|earliest|not before|ei ennen|valmis|\bready\b|efter|tidigast|inte före|klar", re.ASCII)
BEFORE = re.compile(r"ennen|mennessä|viim|\bbefore\b|\bby\b|latest|före|senast", re.ASCII)
DELIVERY = re.compile(r"toimit|perillä|perille|vastaanot|kohde|deliver|arrive|destination|receiv|leverera|leverans|framme|mottag", re.ASCII)
CLOCK = re.compile(r"(?:klo|kl\.?)\s*(\d{1,2})(?:[.:](\d{2}))?|\b(\d{1,2})[.:](\d{2})\b", re.ASCII)
# Opening hours as a range: "9-16", "klo 9.00-14.30", "kl 10-14".
RANGE = re.compile(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*[-–]\s*(\d{1,2})(?:[.:](\d{2}))?\b", re.ASCII)


def window_from_note(note: Optional[Note], window: TimeWindow, stop: str) -> TimeWindow:
    """Narrows a stop's window by clock times in the note, found by keywords: "nouto ennen 15.30",
    "klo 12 jälkeen", "deliver by 19:00", "efter kl. 12", "aikaisintaan klo 13.30", "not before
    16:00", and opening hours as a range, "arkisin 9-16". The time is on the window's own day."""
    if not note:
        return window
    text = note["text"].lower()
    if bool(DELIVERY.search(text)) != (stop == "delivery"):
        return window

    def minutes(hour: int, minute: int) -> Optional[int]:
        return None if hour > 24 or minute > 59 else hour * HOUR + minute

    start, end = window["from"], window["to"]
    # Opening hours hold every day: they move the window's start to the first day's opening and its end to the last day's closing.
    for match in RANGE.finditer(text):
        opens, closes = minutes(int(match[1]), int(match[2] or 0)), minutes(int(match[3]), int(match[4] or 0))
        if opens is None or closes is None or opens >= closes:
            continue
        start = max(start, start // DAY * DAY + opens)
        end = min(end, end // DAY * DAY + closes)
    text = RANGE.sub(" ", text)
    for match in CLOCK.finditer(text):
        m = minutes(int(match[1] or match[3]), int(match[2] or match[4] or 0))
        if m is None:
            continue
        time = window["from"] // DAY * DAY + m
        around = text[max(0, match.start() - 16) : match.end() + 16]
        if AFTER.search(around):
            start = max(start, time)
        elif BEFORE.search(around):
            end = min(end, time)
    return {"from": start, "to": end} if start <= end else window


def play_practice(world: World, level: int, params: Optional[Params] = None, quiet: bool = False) -> "Desk":
    """Steps the world from one event that needs an answer to the next, and stops at each midnight for a report."""
    snapshot = world.snapshot()
    desk, ends_at = Desk(world, level, snapshot["now"], params), snapshot["endsAt"]
    while desk.now < ends_at:
        day, midnight = desk.now // DAY, (desk.now // DAY + 1) * DAY
        # One request a step: the advance applies the outbox, then moves the clock.
        actions, desk.outbox = desk.outbox, []
        stop = min(ends_at, midnight, desk.next_tick() or ends_at)
        step = world.advance(until=WAKE_ON, max=stop, actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]
        for event in step["events"]:
            desk.handle(event)
        desk.tick()
        if desk.now // DAY > day and not quiet:
            report(WEEKDAYS[day % 7], world.score())
    return desk


SEND_EVERY = 0.25  # real seconds: the longest an answer waits in the outbox while events keep coming
CLOCK_EVERY = 1.0  # real seconds between two reads of the world clock while no event comes
SAVE_EVERY = 5.0  # real seconds between two saves of what the desk has learned


def state_path(world: World) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", f"state-{world.id}.json")


def play_final(world: World, params: Params, clock=None) -> None:
    """Rebuilds the books, then answers events as they come, with the world's clock moving by itself.

    Answers are sent in batches: a burst of requests (a bulk order brings 30-40 at once) goes out in one
    request of up to 50 actions instead of one round trip each, and a price request gives only about 4.5
    real seconds. The outbox goes out at the latest SEND_EVERY seconds after it starts to fill, and at once
    when the stream falls quiet. What the desk has learned (prices, dealer trust) is saved to a file every
    few seconds, so a restart picks it up. `clock` reads the world minute; by default the final's clock."""
    clock = clock or (lambda: world.client.final()["now"])
    history = world.events()
    snapshot = world.snapshot()
    if snapshot["now"] >= snapshot["endsAt"]:
        return
    level = next((w["level"] for w in world.client.worlds() if w["worldId"] == world.id), 8)
    desk = Desk(world, level, snapshot["now"], params)
    desk.rejoin(snapshot, history)
    desk.load_state(state_path(world))
    desk.send()
    filled_at, clock_at, saved_at = None, time.monotonic(), time.monotonic()
    try:
        for event in world.stream(after=history[-1]["seq"] if history else -1, idle=SEND_EVERY):
            day, real = desk.now // DAY, time.monotonic()
            if event is not None:
                desk.now = max(desk.now, event["at"])
                desk.handle(event)
            elif real - clock_at >= CLOCK_EVERY:
                desk.now, clock_at = max(desk.now, clock()), real
            desk.tick()
            if desk.outbox and filled_at is None:
                filled_at = real
            if desk.outbox and (event is None or real - filled_at >= SEND_EVERY):
                desk.send()
                filled_at = None
            if real - saved_at >= SAVE_EVERY:
                desk.save_state(state_path(world))
                saved_at = real
            if desk.now // DAY > day:
                report(WEEKDAYS[day % 7], world.score())
            if desk.now >= snapshot["endsAt"]:
                return
    finally:
        print(f"Quotes the world refused (mostly too late): {desk.refused_quotes}. Pairs posted: {desk.paired}.", flush=True)
        desk.save_state(state_path(world))
        with contextlib.suppress(SpreadError):  # after a broken connection the restart loop sends again
            desk.send()


def report(label: str, score: Score) -> None:
    orders = f"{score['wonOrders']:>4} orders  {score['onTimeShare']:6.1%} on time  {score['undeliveredShare']:5.1%} not delivered"
    print(f"{label:<9}  margin €{score['marginCents'] / 100:>11,.2f}  revenue €{score['revenueCents'] / 100:>11,.2f}  {orders}", flush=True)


def next_final(client: Client) -> World:
    """Your copy of the final that is set or runs now. Waits while none is set and while the last one has
    ended: the organisers set a test final 15 minutes before its start, and the final 30 minutes before."""
    said = False
    while True:
        try:
            world: Optional[World] = client.world(client.final()["worldId"])
        except SpreadError as error:
            if error.status != 404:
                raise
            world = None
        if world is not None:
            snapshot = world.snapshot()
            if snapshot["now"] < snapshot["endsAt"]:
                return world
        if not said:
            print("No final is set. Waiting for it: the organisers set a test final 15 minutes before its start, and the final 30 minutes before.", flush=True)
            said = True
        time.sleep(10)


def load_env() -> None:
    """Reads SPREAD_URL and SPREAD_KEY from the .env file next to this script, unless they are already set."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and not name.startswith("#"):
                    os.environ.setdefault(name.strip(), value.strip())


def main() -> None:
    load_env()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the € sign in the reports, on a Windows console
    parser = argparse.ArgumentParser(description="Plays a Spread world with the starter. Set SPREAD_URL and SPREAD_KEY first.")
    parser.add_argument("--level", type=int, choices=range(1, 9), default=8, help="the practice level, 1 to 8 (default 8)")
    parser.add_argument("--seed", help="any text; the same seed gives the same world")
    parser.add_argument("--board", action="store_true", help="play your next ranking run at the level; it counts in the practice rankings")
    parser.add_argument("--final", action="store_true", help="play your copy of the final from the live stream")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="override a strategy parameter (see Params)")
    parser.add_argument("--json", action="store_true", help="no daily lines; print one JSON line with the result at the end")
    args = parser.parse_args()
    params = parse_params(args.param)
    try:
        client = Client()
        score = None
        if args.final:
            world = None
            # A server restart drops every connection. The desk then rebuilds its books from the server, as after its own restart.
            while True:
                try:
                    if world is None:
                        world = next_final(client)
                        print(f"Final world {world.id}.", flush=True)
                    play_final(world, params)
                    score = world.score()
                    break
                except Unreachable as error:
                    print(f"spread: {error}. Trying again in 5 seconds.", file=sys.stderr, flush=True)
                    time.sleep(5)
        else:
            world = client.create(level=args.level, seed=args.seed, board=args.board)
            # A ranking run that a team deletes plays to its end without the team's system, and counts.
            name = f"{'Ranking run' if args.board else 'Practice world'} {world.id}"
            stops = "Ctrl-C or an error stops your system: the run then plays to its end without it, and counts." if args.board else "Ctrl-C or an error stops the run and deletes the world."
            if not args.json:
                print(f"{name}, level {args.level}. {stops}", flush=True)
            try:
                desk = play_practice(world, args.level, params, quiet=args.json)
                if args.json:
                    score = world.score()
                    print(json.dumps({"world": world.id, "level": args.level, "seed": args.seed, "params": asdict(params),
                                      "declined": desk.declined, "paired": desk.paired,
                                      "price_week": round(desk.pricing.week, 3), **score}), flush=True)
                    return
            except BaseException as error:  # a world left behind holds one of the team's eight practice slots
                try:
                    world.delete()
                    deleted = True
                except SpreadError:
                    deleted = False
                after = (f"{name} plays to its end without your system, and counts." if args.board
                         else f"{name} is deleted." if deleted else f"{name} is not deleted: it holds one of your eight practice slots.")
                if isinstance(error, KeyboardInterrupt):
                    sys.exit(f"Stopped. {after}")
                print(f"Stopped by the error below. {after}", file=sys.stderr)
                raise
        score = score or world.score()
        report("End", score)
        print("Ranked." if score["ranked"] else "Not ranked: no order won." if score["wonOrders"] == 0 else "Not ranked: the delivery minimum is 90% of the orders on time.")
    except SpreadError as error:
        sys.exit(f"spread: {error}")
    except KeyboardInterrupt:
        sys.exit("Stopped. The final world keeps running without your system; start it again with --final.")


if __name__ == "__main__":
    main()
