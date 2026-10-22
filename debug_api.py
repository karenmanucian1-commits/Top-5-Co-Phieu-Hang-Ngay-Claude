"""Kiểm tra nhanh dữ liệu API trả về. Chạy: py debug_api.py"""
import json
import time

import vci_client as vci

url = f"{vci.TRADING_URL}/chart/OHLCChart/gap-chart"
for syms in (["FPT"], ["FPT", "VNM"]):
    data = vci._request("POST", url, json={"timeFrame": "ONE_DAY", "symbols": syms,
                                             "to": int(time.time()) + 86400, "countBack": 3})
    print(f"\n=== GIÁ {syms} | kiểu: {type(data).__name__} ===")
    print(json.dumps(data, ensure_ascii=False)[:600])
    print("-> đọc được:", list(vci._fetch_ohlc_batch(syms, 5).keys()))

print("\n=== CHỈ SỐ TÀI CHÍNH FPT ===")
recs = vci.get_ratios("FPT")
print("số bản ghi:", len(recs))
if recs:
    print(json.dumps(recs[0], ensure_ascii=False)[:1500])
