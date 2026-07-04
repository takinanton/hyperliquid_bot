"""Probe yfinance 1h max depth for stocks."""
import warnings; warnings.filterwarnings("ignore")
import yfinance as yf
import pandas as pd

for tk in ["AAPL", "MSFT", "SPY"]:
    for period in ["1y", "2y", "max"]:
        try:
            df = yf.download(tk, period=period, interval="1h",
                             auto_adjust=False, progress=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df = df.xs(tk, axis=1, level=1, drop_level=True)
            if df.empty:
                print(f"  {tk:6} period={period:4}  EMPTY")
                continue
            df.index = pd.to_datetime(df.index, utc=True)
            first = df.index[0]; last = df.index[-1]
            days = (last - first).days
            print(f"  {tk:6} period={period:4}  bars={len(df):>5}  span={str(first)[:10]} -> {str(last)[:10]}  ({days}d)")
        except Exception as e:
            print(f"  {tk:6} period={period:4}  FAIL: {type(e).__name__}: {str(e)[:80]}")
