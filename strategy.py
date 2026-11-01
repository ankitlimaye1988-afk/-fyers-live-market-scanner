import numpy as np
import pandas as pd

def ema(series, length):
    return series.ewm(span=length, adjust=False, min_periods=length).mean()

def stochastic(df, length):
    low = df["low"].rolling(length, min_periods=length).min()
    high = df["high"].rolling(length, min_periods=length).max()
    k = 100 * (df["close"] - low) / (high - low).replace(0, np.nan)
    d = k.rolling(3, min_periods=3).mean()  # (length, 1, 3)
    return k, d

def macd(df):
    line = ema(df["close"], 11) - ema(df["close"], 20)
    signal = line.ewm(span=30, adjust=False, min_periods=30).mean()
    return line, signal, line - signal

def atr(df, length=14):
    previous = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous).abs(),
        (df["low"] - previous).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length, min_periods=length).mean()

def evaluate(df):
    if len(df) < 140:
        return None
    x = df.copy().reset_index(drop=True)
    x["ema20"] = ema(x["close"], 20)
    x["ema100"] = ema(x["close"], 100)
    x["k100"], x["d100"] = stochastic(x, 100)
    x["k14"], x["d14"] = stochastic(x, 14)
    x["macd"], x["macd_signal"], x["hist"] = macd(x)
    x["atr14"] = atr(x)
    a, p = x.iloc[-1], x.iloc[-2]
    needed = ["ema20", "ema100", "k100", "d100", "k14", "d14",
              "macd", "macd_signal", "hist", "atr14"]
    if any(pd.isna(a[k]) for k in needed) or pd.isna(p["hist"]):
        return None

    buy = (a.ema20 > a.ema100 and a.close > a.ema20
           and a.k100 > 50 and a.k100 > a.d100 and a.k100 > p.k100
           and a.k14 > 50 and a.k14 > a.d14 and a.k14 > p.k14
           and a.macd > a.macd_signal and a.hist > p.hist)
    sell = (a.ema20 < a.ema100 and a.close < a.ema20
            and a.k100 < 50 and a.k100 < a.d100 and a.k100 < p.k100
            and a.k14 < 50 and a.k14 < a.d14 and a.k14 < p.k14
            and a.macd < a.macd_signal and a.hist < p.hist)
    if not (buy or sell):
        return None

    side = "BUY" if buy else "SELL"
    entry = float(a.close)
    risk = max(float(a.atr14) * 1.5, 0.01)
    return {
        "signal": side, "entry": round(entry, 2),
        "stop_loss": round(entry - risk if buy else entry + risk, 2),
        "target": round(entry + 2*risk if buy else entry - 2*risk, 2),
        "ema20": round(float(a.ema20), 2),
        "ema100": round(float(a.ema100), 2),
        "stoch100_k": round(float(a.k100), 2),
        "stoch14_k": round(float(a.k14), 2),
        "macd_hist": round(float(a.hist), 4),
        "atr14": round(float(a.atr14), 2)
    }
