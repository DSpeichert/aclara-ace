#!/usr/bin/env python3
"""Pull water usage from the Aclara ACE portal and print it as JSON or CSV.

Credentials come from ACE_USERNAME / ACE_PASSWORD / ACE_CLIENT_ID (env or a .env file).

    python scripts/ace_pull.py --days 30
    python scripts/ace_pull.py --start 2026-01-04 --end 2026-09-04 --csv > water.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "aclara_ace"))
from api import AclaraAceClient  # noqa: E402


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


async def _main(args: argparse.Namespace) -> int:
    for candidate in (Path(args.env) if args.env else None, ROOT / ".env", ROOT.parent / ".env"):
        if candidate:
            _load_env(candidate)
    username = os.environ.get("ACE_USERNAME")
    password = os.environ.get("ACE_PASSWORD")
    client_id = args.client_id or os.environ.get("ACE_CLIENT_ID")
    if not username or not password or not client_id:
        print("Set ACE_USERNAME, ACE_PASSWORD and ACE_CLIENT_ID (env, .env or --client-id)", file=sys.stderr)
        return 2

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days - 1)

    async with aiohttp.ClientSession() as session:
        client = AclaraAceClient(session, username, password, client_id=int(client_id))
        await client.async_login()
        account = await client.async_get_account()
        spans = await client.async_get_meter_spans()
        if args.info:
            print(json.dumps({
                "customer_id": account.customer_id,
                "account_id": account.account_id,
                "name": account.name,
                "timezone": account.timezone,
                "meters": [m.__dict__ for m in account.meters],
                "spans": [
                    {**s.__dict__, "oldest": s.oldest.isoformat(), "newest": s.newest.isoformat()}
                    for s in spans
                ],
            }, indent=2))
            return 0
        meters = [m for m in account.meters if not args.meter or m.meter_id == args.meter]
        if not meters:
            print("No matching meter", file=sys.stderr)
            return 1
        rows = []
        for meter in meters:
            for r in await client.async_get_hourly_usage(meter, start, end):
                rows.append((meter.meter_id, r.start, r.quantity, r.unit))

    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["meter_id", "start", "quantity", "unit"])
        for meter_id, ts, qty, unit in rows:
            w.writerow([meter_id, ts.isoformat(), qty, unit])
    else:
        total = sum(q for _, _, q, _ in rows)
        print(json.dumps({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": len(rows),
            "total": total,
            "unit": rows[0][3] if rows else None,
            "first": rows[0][1].isoformat() if rows else None,
            "last": rows[-1][1].isoformat() if rows else None,
            "readings": [
                {"meter_id": m, "start": ts.isoformat(), "quantity": q} for m, ts, q, _ in rows
            ] if args.full else None,
        }, indent=2))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", help="first local date, YYYY-MM-DD")
    p.add_argument("--end", help="last local date, YYYY-MM-DD (default today)")
    p.add_argument("--days", type=int, default=7, help="days to pull when --start is omitted")
    p.add_argument("--meter", help="only this meter id")
    p.add_argument("--client-id", type=int, help="utility client id from the portal login URL (default: ACE_CLIENT_ID)")
    p.add_argument("--env", help="path to a .env file")
    p.add_argument("--info", action="store_true", help="print account/meter info and exit")
    p.add_argument("--csv", action="store_true", help="emit CSV instead of JSON")
    p.add_argument("--full", action="store_true", help="include every reading in JSON output")
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
