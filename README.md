# NewKaabi

Our dispatch system for the Bahn Express Spread challenge (Junction 2026). The system is in `dispatch.py`. `harness.py` and `analysis/sweep.py` play practice weeks and compare scores.

The game rules are in `HANDBOOK.MD`, `CHALLENGE.md`, and `kit/RULES.md`. Where those disagree, `kit/RULES.md` is the one that counts.

Key dates (Helsinki time): test finals Saturday 14 Nov at 14:00 and 20:00; the final Sunday 15 Nov at 08:00 (registration closes 07:30).

## Setup

Python 3.9 or later. Make a virtual environment in the repo and install the Spread wheel from `kit/` into it. `pandas` is only needed for `analysis/late.py`.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install kit\spread-0.1.0-py3-none-any.whl pandas
```

On macOS or Linux, use `.venv/bin/python` instead of `.venv\Scripts\python`.

Create `.env` in the repo root. `dispatch.py`, `harness.py`, and `analysis/sweep.py` all read it, so there is nothing to export. Do not commit it; it is gitignored.

```
SPREAD_URL=https://spread.bahnexpress.com
SPREAD_KEY=<team key from https://spread.bahnexpress.com/join>
```

The event code is `spread-kmu9z6`.

All commands below run from the repo root with the venv's Python. Plain `python` only works if you activated the venv first (`.venv\Scripts\activate`).

## Play one week

```powershell
.venv\Scripts\python dispatch.py --level 8 --seed spread-1
```

It prints the margin and on-time share after each day and at the end.

| Option | Meaning |
|---|---|
| `--level 1` to `--level 8` | Practice level. Default is 8, the final's level. |
| `--seed <text>` | The same seed gives the same week. Without it, a random week. |
| `--param name=value` | Override one setting from `Params` for this run. Repeat for several. |
| `--board` | Play the team's next **ranking run**. It counts, and deleting it still counts. |
| `--final` | Play the team's copy of the live final (or a test final). |

## Test on many weeks

Two tools do this. Both play practice weeks only and never start a ranking run or the final.

### `harness.py`: score the system, keep the reveals

```powershell
.venv\Scripts\python harness.py --level 2 --seed setup-check --seed spread-1
```

It plays `dispatch.py` on each seed and prints the margin, the on-time share, and whether the week was ranked. A week is ranked when at least one order is won and at least 90% of won orders arrive on time. After each week it saves the reveal and deletes the practice world, so the same seed can be played again.

Results land in `runs/level-<level>-dispatch-<time>/`:

- `summary.csv` has one row per seed
- `<seed>/` holds the reveal CSVs (`requests.csv`, `legs.csv`, `gigs.csv`, and the rest)

`runs/` is gitignored.

| Option | Meaning |
|---|---|
| `--level 1` to `--level 8` | Practice level. Default is 2. |
| `--seed <text>` | Repeat for each seed. Default seeds are `spread-1`, `setup-check`, and `harness-3`. |
| `--system dispatch` | The system in `dispatch.py`, with its default settings. This is the default. |
| `--system starter` | The kit's starter, for a comparison on the same seeds. |
| `--jobs 1` to `--jobs 4` | Practice worlds at once. A team can hold 8 worlds that have not ended. |
| `--out <folder>` | Where to write the summary and the reveal. |

```powershell
.venv\Scripts\python harness.py --level 1 --system starter --seed setup-check --seed spread-1
```

`harness.py` refuses `--board` and `--final`.

### `analysis/sweep.py`: compare settings side by side

Use this to try different values of the settings in `Params` on the same weeks.

```powershell
.venv\Scripts\python analysis\sweep.py --level 8 --seeds spread-1,spread-2,spread-3 --config base: --config tight:max_backlog=12 --config dear:markup_same_day=1.45,markup_next_day=1.45
```

Each `--config` is `name:param=value,param=value` (`base:` means the defaults). It plays every config on every seed, up to 8 weeks at once (`--workers`), then prints each config's mean margin, its lowest on-time share, and how many weeks were ranked. Every result is also appended to `runs/results.jsonl`.

### `analysis/late.py`: why orders were late

```powershell
.venv\Scripts\python analysis\late.py runs\level-8-dispatch-<time>\spread-1
```

Point it at a reveal folder. It breaks the won orders down by option, day, ready hour, drive time, and drivers near the pickup and delivery, with the late share, mean margin, and fee paid for each group.

## What dispatch does

`dispatch.py` is the kit's starter with its decisions changed. The plumbing for the live final (stream, reconnect, rebuild after a restart) is the starter's and is left as it was. Every setting below is in `Params` at the top of the file.

**Which cars to take.** A request gets no winning quote when:

- a leg's drive is longer than 3 hours (`max_drive_min`). At level 8, drives of 3–5 hours were late about 30% of the time and lost money on average;
- the price we would get is below the typical driver fee (`min_price_fee_ratio`);
- it is same-day and the drive leaves under four hours before the deadline (`same_day_slack_min`). From level 4 this applies only when same-day is the only option;
- at levels 1 and 2, it is a trade-in (`no_trade_in_max_level`). A trade-in that never leaves costs twice its price;
- too many won cars, plus quotes that may still win, are waiting for a driver (`max_backlog`). This keeps us from winning more cars than the drivers can move, which matters most at levels 7 and 8.

Up to level 3 every quote wins, so a car is refused by sending no quote. From level 4 a refused car gets a quote far above the list price, which never wins and costs no goodwill.

**Learning which towns lack drivers.** For each pickup town the system keeps a score of how reliably our gigs there find a driver: it starts from how many drivers live within 40 km, then a booking counts as a success and a deadline warning or an expired gig as a failure. Cars from towns scoring below 0.5 are declined (`min_supply`). After a restart in the final, the scores are rebuilt from the event history.

**Pairing cars.** When a car is won, the system looks for one of our waiting cars that starts within 50 km of where the new car ends, or ends near where it starts (`pair_near_km`). If one driver can take both with both deadlines holding (2 hours to spare), it posts them as one gig at 85% of the two typical fees (`pair_fee_share`), since the driver saves a trip. A same-day car never waits for its partner; another car waits at most 2 hours. A pair that nobody books within an hour, or that gets a deadline warning, expires, or is cancelled or changed, goes back to one gig per car. On 8 level 8 weeks this added about €300 a week, better on every week.

**What to quote.** Up to level 3, the list price of the first option. From level 4, the price is learned during the week (`price_learn`): each dealer gets a multiple of the list price, starting at 1.45. A win raises it by 3% and a loss lowers it by 2%, within 1.3–1.9 (`price_up`, `price_down`, `price_min`, `price_max`). A dealer we have not quoted yet starts from the week's level, which learns from every dealer. Declines do not count as losses. On top, dealer trust (`trust`): +1% on a dealer's prices for each order delivered to it on time, up to 25%; a late order halves the count. With learning off, every option is the list price times a fixed markup (`markup_*`, 1.45). At level 8 the competitor is 26–54% above the list price.

**What to pay the driver.** Up to level 6: one fee, at the typical fee, and one step of 15% after the deadline warning. This does not teach drivers to wait for raises. Levels 7 and 8: a rising fee schedule (`fee_open`, `fee_rise`, `fee_every`, `fee_cap`), and a steeper one after the deadline warning (`steep_*`), because a typical fee often finds no driver there.

## Results so far

| Level | Weeks | Margin per week | Lowest on-time |
|---|---|---|---|
| 1 | spread-1 | €4,509 | 97.1% |
| 2 | spread-1, setup-check, harness-3 | €4,600–5,600 | 97.1% |
| 8 | tune-1 … tune-8 (current defaults) | €3,680–7,730, mean €6,125 | 95.6% (8 of 8 ranked) |

The starter loses €72,612 at level 8 on spread-1, at 45.5% on time. At level 8 we want at least 93% on every week for a safety margin; the next step is learning, during the week, which towns lack drivers.

Level 8 settings compared on the same 8 weeks:

| Settings | Ranked | Lowest on-time | Mean margin |
|---|---|---|---|
| 5 h drives, `max_backlog=12` | 5 of 8 | 87.0% | €3,868 |
| same, with fees 1.2× rising 5%/h to 1.6× | 7 of 8 | 87.8% | €1,575 |
| 3 h drives, `max_backlog=12` | 8 of 8 | 90.2% | €4,129 |
| 3 h drives, `max_backlog=10` | 8 of 8 | 91.7% | €4,057 |
| 4 h drives, `max_backlog=12` | 7 of 8 | 86.0% | €4,003 |
| + decline towns where gigs do not book (`min_supply=0.5`) | 8 of 8 | 92.2% | €4,154 |
| + pairing within 50 km | 8 of 8 | 92.0% | €4,462 |
| + quotes at 1.45× list instead of 1.3× | 8 of 8 | 92.6% | €5,769 |
| same, quotes at 1.6× list | 8 of 8 | 94.9% | €5,358 (€8,226 in a dear week, €2,685 in a cheap one) |
| **+ price learning per dealer and dealer trust, floor 1.3× (current default)** | **8 of 8** | **95.6%** | **€6,125** |
