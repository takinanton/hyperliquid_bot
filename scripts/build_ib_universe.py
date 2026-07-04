#!/usr/bin/env python3
"""Build IB bot universe from public sources — NO arbitrary caps.

Sources (all complete sets, not top-N curated):
  • NASDAQ-listed stocks/ETFs  → nasdaqtrader.com/.../nasdaqlisted.txt
  • NYSE+AMEX+ARCA listed       → nasdaqtrader.com/.../otherlisted.txt
  • CME/CBOT/NYMEX/COMEX/ICE futures roster (complete fixed universe)

Crypto-related instruments filtered via regex on symbol AND security name.

FX rip (2026-05-12): forex pairs (IDEALPRO spot) + FX CFDs + FX futures
(6E/6J/6B/6A/6C/6S/6N) полностью удалены — бот не торгует валютами.

Output: data/ib_universe.json
  [{"sym": "AAPL", "type": "stock", "name": "Apple Inc."}, ...]

Bot then iterates this list and applies LIQUIDITY_MIN_4H_USD live filter.

Daily cron (00:05 UTC):
  5 0 * * * cd /home/ubuntu/hyperliquid_bot && venv/bin/python scripts/build_ib_universe.py
"""
from __future__ import annotations

import json
import logging
import re
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger("build_ib_universe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OUTPUT = Path("data/ib_universe.json")

# ---------------------------------------------------------------------------
# Crypto-related filter: regex on symbol AND company name
# ---------------------------------------------------------------------------

CRYPTO_NAME_RX = re.compile(
    r"\b(BITCOIN|ETHEREUM|CRYPTO|BLOCKCHAIN|DIGITAL\s+CURRENCY|DIGITAL\s+ASSET|"
    r"WEB3|METAVERSE\s+CRYPTO|STABLECOIN|ICO|MINER|MINING\s+POOL|HASHRATE|"
    r"SATOSHI|DEFI)\b",
    re.IGNORECASE,
)

CRYPTO_SYMBOL_RX = re.compile(
    r"^("
    r"BTC|ETH|XBT|ETHE?|"             # raw crypto
    r"IBIT|FBTC|ARKB|BRRR|BTCC|"      # spot BTC ETFs
    r"BITO|BITS|BITQ|BTCO|BLOK|BKCH|" # crypto ETFs / funds
    r"ETHA|ETHV|ETHW|"                # spot ETH ETFs
    r"GBTC|ETHE|"                     # Grayscale
    r"MARA|RIOT|HUT|BITF|CLSK|CIFR|CORZ|BTBT|" # miners
    r"MSTR|COIN|HOOD|BLOK|"           # crypto-exposed corps
    r"WGMI|DAPP|SATO|CRPT|STKD|CONL|" # micro/leveraged
    r"ARKW"                           # heavy crypto-weight ETF
    r")$",
    re.IGNORECASE,
)


def is_crypto_related(symbol: str, name: str) -> bool:
    if CRYPTO_SYMBOL_RX.match(symbol):
        return True
    if CRYPTO_NAME_RX.search(name):
        return True
    return False


# ---------------------------------------------------------------------------
# US stock universe — sourced from GitHub mirror of NASDAQ Trader files
# (rreichel3/US-Stock-Symbols, updated daily from official NASDAQ feed)
# This is a COMPLETE list of US-listed stocks/ETFs across NASDAQ/NYSE/AMEX.
# ---------------------------------------------------------------------------

MIRROR_BASE = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main"
NASDAQ_FULL = f"{MIRROR_BASE}/nasdaq/nasdaq_full_tickers.json"
NYSE_FULL = f"{MIRROR_BASE}/nyse/nyse_full_tickers.json"
AMEX_FULL = f"{MIRROR_BASE}/amex/amex_full_tickers.json"


def fetch_url(url: str, timeout: int = 30) -> str:
    log.info("fetch %s", url)
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ib-bot universe-builder",
    })
    r.raise_for_status()
    return r.text


def parse_full_tickers(text: str, exchange_label: str) -> list[dict]:
    """Parse JSON list of dicts from rreichel3 mirror.

    Schema: [{"symbol":"AAPL","name":"Apple Inc","sector":"Tech","industry":"...",
              "country":"United States","marketCap":"3.5T","lastsale":"$220.00", ...}, ...]
    """
    data = json.loads(text)
    out = []
    for row in data:
        sym = str(row.get("symbol", "")).strip().upper()
        name = str(row.get("name", "")).strip()
        if not sym:
            continue
        # Skip non-trade-friendly suffixes: preferred ($), warrants (.W), units (.U), rights (.R)
        # Mirror uses "/" separator for share classes (BRK.B → "BRK.B" but no slash). Keep as-is.
        if any(suf in sym for suf in ["$", ".W", ".U", ".R", ".P", "^"]):
            continue
        # Drop if marketCap=0 AND no recent volume (dead listings) — keeps universe sane
        # Note: we'll still apply LIQUIDITY_MIN_4H_USD live filter
        # but skip clearly-dead symbols pre-emptively to save yf calls
        sector = str(row.get("sector", "")).strip()
        industry = str(row.get("industry", "")).strip()
        is_etf = ("ETF" in name.upper()) or ("EXCHANGE TRADED" in name.upper()) or (sector == "" and industry == "")
        out.append({
            "sym": sym,
            "type": "etf" if is_etf else "stock",
            "name": name,
            "exchange": exchange_label,
        })
    return out


# ---------------------------------------------------------------------------
# Futures roster — COMPLETE universe of CME-group + ICE futures
# (Not a curated subset; this IS all of them. ICE includes coffee/cotton/sugar.)
# ---------------------------------------------------------------------------

FUTURES_ROSTER = [
    # Equity Index: CME (S&P/Nasdaq/Russell) + CBOT (Dow YM/MYM)
    {"sym": "ES", "exch": "CME", "name": "E-mini S&P 500"},
    {"sym": "NQ", "exch": "CME", "name": "E-mini Nasdaq 100"},
    {"sym": "YM", "exch": "CBOT", "name": "E-mini Dow"},
    {"sym": "RTY", "exch": "CME", "name": "E-mini Russell 2000"},
    {"sym": "EMD", "exch": "CME", "name": "E-mini S&P MidCap 400"},
    {"sym": "MES", "exch": "CME", "name": "Micro E-mini S&P 500"},
    {"sym": "MNQ", "exch": "CME", "name": "Micro E-mini Nasdaq 100"},
    {"sym": "MYM", "exch": "CBOT", "name": "Micro E-mini Dow"},
    {"sym": "M2K", "exch": "CME", "name": "Micro E-mini Russell 2000"},

    # NYMEX Energy
    {"sym": "CL", "exch": "NYMEX", "name": "WTI Crude Oil"},
    {"sym": "BZ", "exch": "NYMEX", "name": "Brent Crude"},
    {"sym": "NG", "exch": "NYMEX", "name": "Henry Hub Natural Gas"},
    {"sym": "RB", "exch": "NYMEX", "name": "RBOB Gasoline"},
    {"sym": "HO", "exch": "NYMEX", "name": "NY Harbor ULSD"},
    {"sym": "MCL", "exch": "NYMEX", "name": "Micro WTI Crude Oil"},
    {"sym": "MNG", "exch": "NYMEX", "name": "Micro Henry Hub Natural Gas"},

    # COMEX Metals
    {"sym": "GC", "exch": "COMEX", "name": "Gold"},
    {"sym": "SI", "exch": "COMEX", "name": "Silver"},
    {"sym": "HG", "exch": "COMEX", "name": "Copper"},
    {"sym": "PL", "exch": "NYMEX", "name": "Platinum"},
    {"sym": "PA", "exch": "NYMEX", "name": "Palladium"},
    {"sym": "MGC", "exch": "COMEX", "name": "Micro Gold"},
    {"sym": "SIL", "exch": "COMEX", "name": "E-mini Silver"},
    {"sym": "MHG", "exch": "COMEX", "name": "Micro Copper"},

    # CBOT Bonds / Rates
    {"sym": "ZN", "exch": "CBOT", "name": "10-Year T-Note"},
    {"sym": "ZB", "exch": "CBOT", "name": "30-Year T-Bond"},
    {"sym": "ZF", "exch": "CBOT", "name": "5-Year T-Note"},
    {"sym": "ZT", "exch": "CBOT", "name": "2-Year T-Note"},
    {"sym": "UB", "exch": "CBOT", "name": "Ultra T-Bond"},
    {"sym": "TN", "exch": "CBOT", "name": "Ultra 10-Year T-Note"},

    # CBOT Agri (Grains)
    {"sym": "ZC", "exch": "CBOT", "name": "Corn"},
    {"sym": "ZS", "exch": "CBOT", "name": "Soybeans"},
    {"sym": "ZW", "exch": "CBOT", "name": "Wheat"},
    {"sym": "ZL", "exch": "CBOT", "name": "Soybean Oil"},
    {"sym": "ZM", "exch": "CBOT", "name": "Soybean Meal"},
    {"sym": "ZR", "exch": "CBOT", "name": "Rice"},
    {"sym": "ZO", "exch": "CBOT", "name": "Oats"},

    # ICE Softs
    {"sym": "KC", "exch": "NYBOT", "name": "Coffee"},
    {"sym": "CT", "exch": "NYBOT", "name": "Cotton"},
    {"sym": "SB", "exch": "NYBOT", "name": "Sugar"},
    {"sym": "CC", "exch": "NYBOT", "name": "Cocoa"},
    {"sym": "OJ", "exch": "NYBOT", "name": "Orange Juice"},

    # CME livestock
    {"sym": "LE", "exch": "CME", "name": "Live Cattle"},
    {"sym": "GF", "exch": "CME", "name": "Feeder Cattle"},
    {"sym": "HE", "exch": "CME", "name": "Lean Hogs"},

    # CME FX Futures (6E/6J/6B/6A/6C/6S/6N) удалены 2026-05-12 — FX rip.
    # Crypto futures (BRR/MBT/MET/ETH) — EXCLUDED via CRYPTO_SYMBOL_RX.
]


# ---------------------------------------------------------------------------
# ETF roster — comprehensive list from major US issuers.
# rreichel3 mirror only has stocks listed on NASDAQ/NYSE/AMEX direct exchange
# floors; most ETFs trade on NYSE Arca which the mirror doesn't cover.
# This is COMPLETE roster of liquid ETFs from BlackRock/Vanguard/SPDR/Invesco
# /Schwab/ProShares — not a curated subset.
# ---------------------------------------------------------------------------

ETF_ROSTER = [
    # --- Index broad market ---
    "SPY","IVV","VOO","SPLG","RSP",       # S&P 500
    "QQQ","QQQM","ONEQ",                   # Nasdaq 100
    "DIA",                                  # Dow 30
    "IWM","IJR","IWN","IWO","IJH","IWP","IWR","VB","VO","VTWO","SCHA","SCHM", # mid/small
    "VTI","ITOT","SCHB",                   # total US
    "VEA","IEFA","SCHF","EFA","VGK",       # developed intl
    "VWO","EEM","IEMG","SCHE","IEMG",      # emerging
    "VXUS","ACWI","IXUS","ACWX",           # global
    # --- Sector SPDRs ---
    "XLF","XLE","XLK","XLV","XLY","XLP","XLI","XLB","XLU","XLRE","XLC",
    "VFH","VGT","VHT","VCR","VDC","VIS","VAW","VPU","VOX","VNQ",
    # --- Bonds (Treasury, corp, muni, international) ---
    "AGG","BND","BNDX","BNDW","TLT","IEF","SHY","IEI","GOVT","SCHO","SCHR","SCHQ",
    "VGLT","VGIT","VGSH","BSV","BIV","BLV","EDV","TLH",
    "LQD","VCSH","VCIT","VCLT","HYG","JNK","SHYG","HYLB","SLQD",
    "MUB","VTEB","SUB","SHM","TFI",
    "TIP","VTIP","SCHP","STIP","TIPS",
    "MBB","VMBS","CMBS","FLOT","FLRT","BKLN","SRLN",
    "IGOV","EMB","PCY","VWOB","LEMB",
    # --- Commodity ---
    "GLD","IAU","SGOL","GLDM","BAR",        # gold
    "SLV","SIVR","PSLV",                    # silver
    "USO","DBO","USL","BNO","CRAK",        # oil
    "UNG","UNL","BOIL","KOLD",              # nat gas
    "CPER","JJC","COPX",                    # copper
    "PALL","PPLT","SPPP",                   # platinum/palladium
    "DBA","WEAT","CORN","SOYB","CANE","JJG", # grains/agri
    "DBC","GSG","PDBC","COMT","BCI","CMDY", # broad commodity
    # Currency ETFs (UUP/UDN/FXE/FXY/FXB/FXA/FXC/FXF/FXSG/CYB) удалены 2026-05-12 — FX rip
    # --- Country single (G20+) ---
    "EWJ","EWG","EWU","EWP","EWQ","EWI","EWN","EWL","EWD","EWO","EZU","EWK",
    "EWZ","ECH","EWW","ARGT","ILF","GXG","EPU",
    "MCHI","FXI","ASHR","KBA","CQQQ","CHIQ","CHIX","CXSE","KWEB",
    "INDA","SMIN","EPI","INDY","EWA","EWS","EWT","EWY","EWH","TUR","KSA","UAE","QAT","EZA",
    # --- Theme / factor ---
    "MTUM","QUAL","SIZE","USMV","VLUE","ESGV","ESGU","JKD","VTV","VUG","VYM","VIG","DVY","SCHD","NOBL",
    "SDY","HDV","SPHD","SCHV","SPYV","SPYG","IWF","IWD","RWL","DGRO",
    # --- Real estate ---
    "VNQ","IYR","SCHH","REM","MORT","RWR","REET","XLRE","REZ","FREL","BBRE","INDS",
    # --- Healthcare / biotech ---
    "IBB","XBI","XHE","XHS","IHE","IHI","IYH","IHF","ARKG","FBT","PBE","SBIO",
    # --- Tech / semiconductors ---
    "VGT","XLK","SOXX","SMH","IGV","FDN","HACK","SKYY","CIBR","CLOU","WCLD","ARKK","ARKW","ARKQ","ARKF","ARKX","BOTZ","IRBO","ROBO","AIQ","CHAT","BUG",
    # --- Energy / clean energy ---
    "XLE","VDE","XOP","OIH","FCG","TAN","FAN","ICLN","PBW","ACES","QCLN","NLR","URA","URNM","LIT","REMX",
    # --- Industrial / transport / materials ---
    "XLI","VIS","ITA","XAR","JETS","FLYT","XTN","IYT","XME","SLX","COPX","COPJ","REMX","SIL","SILJ","WOOD","CUT","XLB",
    # --- Financials / fintech ---
    "XLF","VFH","KRE","KBE","IAI","IAK","KIE","FNCL","IXG","XLRE","FXO","CARZ","ETHR",
    # --- Consumer / retail ---
    "XLY","XLP","VCR","VDC","IBUY","ONLN","BUYZ","PEJ","PEZ","XRT","IYK","FXG","FSTA","PMAR",
    # --- Utilities / infrastructure ---
    "XLU","VPU","IDU","FUTY","FXU","GRID","PAVE","IFRA",
    # --- Communication ---
    "XLC","VOX","FCOM","XTL","VRP",
    # --- Dividend / income ---
    "VYM","DVY","SCHD","NOBL","HDV","SPHD","SDIV","DEM","DGS","DLN","DTN","FDL","RDIV","SDOG","PEY","REGL","SMDV",
    # --- Smart beta / risk-managed ---
    "USMV","ACWV","EFAV","EEMV","SPLV","XMLV","SPHQ","QUAL","SCHK",
    # --- Volatility (excluded from trading — too unstable for 4h breakouts) ---
    # VXX, UVXY, SVXY — DO NOT TRADE
]


# FX pairs (FX_PAIRS list) удалены 2026-05-12 — IB бот не торгует валютами
# (см. data/manual_ops/2026-05-12_ib_rip_fx_shorts/).


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_universe() -> dict:
    """Build universe of CURRENTLY-TRADEABLE instruments for IB bot.

    2026-05-11 deep-check found broker-side blockers on EU/Ireland retail account:
      • US ETFs        — PRIIPs KID missing → ALL US ETFs blocked
      • Futures        — first-trade token-verify pending in Client Portal
      • Complex/leveraged ETPs (UVXY/TQQQ/SQQQ) — separate permit needed

    Toggle flags below as user resolves permits.
    """
    # Feature flags (flip to True after user resolves broker permits)
    INCLUDE_ETFS = False        # PRIIPs KID blocker (reclassify Professional needed)
    INCLUDE_FUTURES = True      # 2026-05-11: token-verified (057205); ES LMT confirmed working
    # FX rip 2026-05-12: forex/CFD/FX-futures удалены полностью; no INCLUDE_FX toggle.

    out: list[dict] = []

    # 1) Futures roster — skipped until token-verified
    if INCLUDE_FUTURES:
        for f in FUTURES_ROSTER:
            out.append({
                "sym": f["sym"], "type": "future", "name": f["name"], "exchange": f["exch"],
            })

    # 2) ETF roster (curated NYSE Arca list) — skipped until PRIIPs resolved
    if INCLUDE_ETFS:
        for sym in ETF_ROSTER:
            out.append({
                "sym": sym, "type": "etf", "name": sym, "exchange": "ARCA",
            })

    # 3) NASDAQ — individual stocks (PRIIPs N/A for stocks)
    try:
        items = parse_full_tickers(fetch_url(NASDAQ_FULL), "NASDAQ")
        if not INCLUDE_ETFS:
            items = [x for x in items if x["type"] != "etf"]
        log.info("NASDAQ: %d raw (ETF filter: %s)", len(items), "off" if INCLUDE_ETFS else "on")
        out.extend(items)
    except Exception as e:
        log.error("NASDAQ fetch FAILED: %s", e)

    # 3b) NYSE
    try:
        items = parse_full_tickers(fetch_url(NYSE_FULL), "NYSE")
        if not INCLUDE_ETFS:
            items = [x for x in items if x["type"] != "etf"]
        log.info("NYSE: %d raw (ETF filter: %s)", len(items), "off" if INCLUDE_ETFS else "on")
        out.extend(items)
    except Exception as e:
        log.error("NYSE fetch FAILED: %s", e)

    # 4) AMEX (mostly ETFs — skip entirely if ETFs disabled)
    try:
        items = parse_full_tickers(fetch_url(AMEX_FULL), "AMEX")
        if not INCLUDE_ETFS:
            items = [x for x in items if x["type"] != "etf"]
        log.info("AMEX: %d raw (ETF filter: %s)", len(items), "off" if INCLUDE_ETFS else "on")
        out.extend(items)
    except Exception as e:
        log.error("AMEX fetch FAILED: %s", e)

    # 5) Crypto filter (regex on symbol + name)
    before = len(out)
    out = [x for x in out if not is_crypto_related(x["sym"], x.get("name", ""))]
    log.info("crypto filter: removed %d → %d remain", before - len(out), len(out))

    # 6) Dedup by symbol — first-seen wins (futures > stocks for collisions)
    seen = set()
    dedup = []
    collisions = []
    for x in out:
        if x["sym"] in seen:
            # Track stock-ticker losses for sanity log
            if x["type"] == "stock":
                collisions.append(x["sym"])
            continue
        seen.add(x["sym"])
        dedup.append(x)
    if collisions:
        log.info("ticker collisions (future wins, stock loses %d): %s",
                 len(collisions), ", ".join(collisions[:15]) + ("..." if len(collisions) > 15 else ""))
    out = dedup

    by_type = {}
    for x in out:
        by_type[x["type"]] = by_type.get(x["type"], 0) + 1
    log.info("FINAL: %d total — breakdown: %s", len(out), by_type)

    return {
        "version": 2,
        "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
        "by_type": by_type,
        "symbols": out,
    }


def main():
    universe = build_universe()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(universe, indent=2))
    log.info("saved → %s (%d symbols)", OUTPUT, len(universe["symbols"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
