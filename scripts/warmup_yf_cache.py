#!/usr/bin/env python3
"""Pre-warm yfinance bar cache for ALL symbols in data/ib_universe.json.

Reduces bot cold-start from ~60 min (sequential single-symbol) to ~5-10 min
(batch downloads of 50 symbols per yf.download call).

Run after `build_ib_universe.py`:
  python scripts/warmup_yf_cache.py

Or via systemd ExecStartPre on ib-bot.service.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.data_source import get_yf_source

log = logging.getLogger("warmup_yf_cache")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    universe_file = Path("data/ib_universe.json")
    if not universe_file.exists():
        log.error("universe file not found — run scripts/build_ib_universe.py first")
        return 1

    data = json.loads(universe_file.read_text())
    symbols = [x["sym"] for x in data["symbols"]]
    log.info("warming up %d symbols", len(symbols))

    src = get_yf_source()
    start = time.time()
    for interval in ("1h", "4h", "1d"):
        t0 = time.time()
        ok = src.warmup_batch(symbols, interval, batch_size=50)
        log.info("  %s: %d bars cached in %.1fs", interval, ok, time.time() - t0)

    log.info("total warmup: %.1fs", time.time() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
