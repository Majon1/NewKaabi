"""Plays dispatch.py on several seeds and settings at once and compares them.

Usage:
  python analysis/sweep.py --seeds s1,s2,s3 --level 8 \
      --config base: --config tight:max_backlog=15 --config dear:markup_same_day=1.45,markup_next_day=1.45

Each config is NAME:param=value,param=value. Results go to runs/results.jsonl too.
At most --workers worlds run at once (the team holds at most 8 practice worlds).
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(name: str, params: list[str], seed: str, level: int, env: dict) -> dict:
    cmd = [sys.executable, str(ROOT / "dispatch.py"), "--level", str(level), "--seed", seed, "--json"]
    for p in params:
        cmd += ["--param", p]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env, encoding="utf-8")
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    if out.returncode != 0 or not lines:
        print(f"[{name} {seed}] failed:\n{out.stderr[-1500:]}", file=sys.stderr, flush=True)
        return {"config": name, "seed": seed, "error": out.stderr[-500:]}
    result = {"config": name, **json.loads(lines[-1])}
    print(f"[{name:>10} {seed:>10}] margin €{result['marginCents']/100:>10,.0f}  on time {result['onTimeShare']:6.1%}  "
          f"orders {result['wonOrders']:>4}  declined {result['declined']:>4}  pairs {result.get('paired', 0):>3}", flush=True)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="spread-1,spread-2,spread-3")
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--config", action="append", default=[])
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    configs = []
    for c in args.config or ["base:"]:
        name, _, rest = c.partition(":")
        configs.append((name, [p for p in rest.split(",") if p]))
    seeds = args.seeds.split(",")
    env = load_env()
    jobs = [(n, p, s) for n, p in configs for s in seeds]
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda j: run(j[0], j[1], j[2], args.level, env), jobs))
    (ROOT / "runs").mkdir(exist_ok=True)
    with open(ROOT / "runs" / "results.jsonl", "a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print("\nconfig        mean margin   min on-time   ranked")
    for name, _ in configs:
        rs = [r for r in results if r["config"] == name and "error" not in r]
        if not rs:
            continue
        mean = sum(r["marginCents"] for r in rs) / len(rs) / 100
        print(f"{name:<12} €{mean:>11,.0f}   {min(r['onTimeShare'] for r in rs):>9.1%}   {sum(r['ranked'] for r in rs)}/{len(rs)}")


if __name__ == "__main__":
    main()
