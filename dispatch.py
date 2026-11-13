"""A practice dispatch policy on top of the starter.

At levels 1–3 the dealer ignores the price and every quote wins the first option,
so a car is declined by sending no quote. Two kinds of car get no quote: the typical
fee is already above the list price, or a same-day car whose drive leaves under four
hours before the deadline. Those same-day cars arrive late often enough to give the
price back.

Up to level 6 the typical fee is enough for most gigs. One fee, with a single small
step after the deadline warning, does not teach drivers to wait for the hourly ladder.
Levels 7 and 8 keep the starter's fee ladder, because a typical fee often finds no driver.
"""
from __future__ import annotations

from spread.starter import DAY, WAKE_ON, WEEKDAYS, Desk as StarterDesk, report
from spread.types import FeeStep, Order, PriceRequest


class Desk(StarterDesk):
    def __init__(self, world, level: int, now: int):
        super().__init__(world, level, now)
        roads = world.client.map()["roads"]
        self.drive_minutes = {(road["from"], road["to"]): road["minutes"] for road in roads}

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        first = request["options"][0]
        price = first["listPriceCents"]
        if self.level <= 3 and (request["referenceFeeCents"] > price or self.same_day_too_tight(request, first["deadline"])):
            return
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": {first["kind"]: price}})

    def same_day_too_tight(self, request: PriceRequest, deadline: int) -> bool:
        """True when the first option is same-day and the drive leaves under four hours."""
        if request["options"][0]["kind"] != "same_day":
            return False
        drive = sum(self.drive_minutes.get((leg["pickup"]["placeId"], leg["delivery"]["placeId"]), 0) for leg in request["legs"])
        return deadline - request["arrivedAt"] - drive < 4 * 60

    def fees_for(self, order: Order, cars: int) -> list[FeeStep]:
        if self.level >= 7:
            return super().fees_for(order, cars)
        fee = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        factor = 1.15 if order["id"] in self.urgent else 1.0
        return [{"at": self.now, "feeCents": max(1, int(fee * factor + 0.5))}]


def play_practice(world, level: int) -> None:
    """The starter's practice loop, with this desk."""
    snapshot = world.snapshot()
    desk, ends_at = Desk(world, level, snapshot["now"]), snapshot["endsAt"]
    while desk.now < ends_at:
        day, midnight = desk.now // DAY, (desk.now // DAY + 1) * DAY
        actions, desk.outbox = desk.outbox, []
        step = world.advance(until=WAKE_ON, max=min(ends_at, midnight), actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]
        for event in step["events"]:
            desk.handle(event)
        if desk.now // DAY > day:
            report(WEEKDAYS[day % 7], world.score())
