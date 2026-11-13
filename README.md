# NewKaabi

Practice dispatch for the Bahn Express Spread challenge. The policy is in `dispatch.py`. `harness.py` plays practice weeks and writes the scores.

The game rules are in `HANDBOOK.MD`, `CHALLENGE.md`, and `kit/RULES.md`. Where those disagree, `kit/RULES.md` is the one that counts.

## Setup

Python 3.9 or later. The Spread package is the wheel in `kit/`.

```powershell
python -m pip install kit\spread-0.1.0-py3-none-any.whl
```

Create `.env` in the repo root. `harness.py` reads it. Do not commit it.

```
SPREAD_URL=https://spread.bahnexpress.com
SPREAD_KEY=<team key from https://spread.bahnexpress.com/join>
```

The event code is `spread-kmu9z6`.

## Run a practice week

```powershell
python harness.py --level 2 --seed setup-check --seed spread-1
```

That plays `dispatch.py` on two practice weeks. The same seed and level can be played again. Each run prints the margin, the on-time share, and whether the week was ranked. A week is ranked when at least one order is won and at least 90% of won orders arrive on time.

Results land in `runs/level-<level>-dispatch-<time>/`:

- `summary.csv` has one row per seed
- `<seed>/` holds the reveal CSVs (`requests.csv`, `legs.csv`, `gigs.csv`, and the rest)

`runs/` is gitignored.

Useful options:

| Option | Meaning |
|---|---|
| `--level 1` to `--level 8` | Practice level. Default is 2. |
| `--seed <text>` | Repeat for each seed. Default seeds are `spread-1`, `setup-check`, and `harness-3`. |
| `--system dispatch` | The policy in `dispatch.py`. This is the default. |
| `--system starter` | The kit's starter, for a comparison on the same seeds. |
| `--jobs 1` to `--jobs 4` | Practice worlds at once. A team can hold 8 worlds that have not ended. |
| `--out <folder>` | Where to write the summary and the reveal. |

```powershell
python harness.py --level 1 --system starter --seed setup-check --seed spread-1
```

`harness.py` refuses `--board` and `--final`. A ranking run is a secret week that counts once for the current round, and deleting it still counts. The Sunday final is a separate week. Neither is started from this harness.

## What dispatch does

`dispatch.py` uses the starter's loop and changes two decisions.

Which cars to take, at levels 1–3. A quote wins the first option, so a car is refused by sending no quote.

- The typical driver fee is already above the list price.
- The car is same-day and the drive leaves under four hours before the deadline.
- The car is same-day and the drive is 7.5 hours or more.
- At levels 1 and 2, the request is a trade-in. A trade-in that never leaves costs twice its price.

What to pay the driver, up to level 6. One fee, at the typical fee. After the deadline warning, one step of 15%. Levels 7 and 8 still use the starter's rising fee, because a typical fee often finds no driver there.
