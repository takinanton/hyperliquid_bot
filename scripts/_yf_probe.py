"""Quick yfinance probe — runs on komp to debug fetch failures."""
import sys, traceback
try:
    import yfinance as yf
    print(f"yf version: {yf.__version__}")
    for tk in ["AAPL", "EURUSD=X", "ES=F", "^VIX"]:
        try:
            df = yf.download(tk, period="1y", interval="1h",
                             auto_adjust=False, progress=False, threads=False)
            print(f"  {tk}: type={type(df).__name__} shape={getattr(df,'shape','?')} "
                  f"cols={list(df.columns)[:6] if hasattr(df,'columns') else 'none'}")
        except Exception as e:
            print(f"  {tk}: FAIL {type(e).__name__}: {e}")
            traceback.print_exc()
except Exception as e:
    print(f"OUTER FAIL: {type(e).__name__}: {e}")
    traceback.print_exc()
