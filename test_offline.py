"""
Chạy thử toàn bộ bot bằng DỮ LIỆU GIẢ (không cần mạng) để kiểm tra logic.
python test_offline.py
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import analysis as an
import main

rng = np.random.default_rng(7)
DATES = pd.bdate_range(end="2026-10-22", periods=320)
SECTORS = ["Ngân hàng", "Bất động sản", "Thép", "Bán lẻ", "Công nghệ", "Thực phẩm"]


def make_series(drift, vol, base, liq):
    r = rng.normal(drift, vol, len(DATES))
    close = base * np.exp(np.cumsum(r))
    high = close * (1 + np.abs(rng.normal(0, vol / 2, len(DATES))))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, len(DATES))))
    openp = (high + low) / 2
    volume = (liq / (close * 1000) * rng.uniform(0.5, 1.5, len(DATES))).astype(int)
    return pd.DataFrame({"date": DATES, "open": openp, "high": high, "low": low,
                         "close": close, "volume": volume})


class FakeClient:
    def __init__(self, n=150):
        self.syms = [f"S{i:02d}"[:3] if i < 100 else f"T{i - 100:02d}" for i in range(n)]
        self.syms = [f"{chr(65 + i // 26)}{chr(65 + i % 26)}X" for i in range(n)]
        self.data = {}
        for s in self.syms:
            drift = rng.choice([-0.001, 0.0, 0.0008, 0.0015])
            liq = rng.choice([1e9, 5e9, 2e10])
            self.data[s] = make_series(drift, 0.02, rng.uniform(8, 80), liq)
        self.data["VNINDEX"] = make_series(0.0004, 0.01, 1200, 1e13)

    def get_all_symbols(self):
        return pd.DataFrame({"symbol": self.syms, "exchange": "HSX", "name": ""})

    def get_industries(self):
        return {s: {"sector": SECTORS[i % len(SECTORS)], "com_type": "NH" if i % 6 == 0 else "CT"}
                for i, s in enumerate(self.syms)}

    def get_ohlc(self, symbols, bars=None):
        return {s: self.data[s] for s in symbols if s in self.data}

    def get_ratios(self, symbol):
        recs = []
        eps0 = rng.uniform(-500, 5000)
        for k in range(8):
            y, q = 2026 - (k + 1) // 4, 4 - (k + 1) % 4
            recs.append({"year": y, "quarter": q, "roe": rng.uniform(0.02, 0.28),
                         "pe": rng.uniform(5, 30), "pb": rng.uniform(0.8, 4),
                         "debtToEquity": rng.uniform(0.1, 2.5),
                         "afterTaxProfitMargin": rng.uniform(0.02, 0.25),
                         "earningPerShare": eps0 * (1 - 0.05 * k)})
        return recs


if __name__ == "__main__":
    fake = FakeClient()
    main.load_cache = lambda: {}
    main.load_universe = lambda now: None
    main.save_universe = lambda now, syms: None
    main.config.REQUEST_SLEEP = 0
    # Lượt CHIỀU (sau đóng cửa)
    ctx = main.run(dry=True, client=fake,
                   now=datetime(2026, 10, 22, 15, 20, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    # Kiểm tra logic vùng giá cho mọi mã trong TOP
    for x in ctx["top"]:
        z = x["z"]
        assert z["stop"] < z["zone_low"] <= z["zone_high"] < z["t1"] <= z["t2"], (x["symbol"], z)
        assert z["risk_pct"] <= 7.01, z
    print("\n=== Lượt SÁNG (dữ liệu dở dang) ===")
    main.run(dry=True, client=fake,
             now=datetime(2026, 10, 22, 11, 35, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    print("OK – logic chạy thông suốt")
