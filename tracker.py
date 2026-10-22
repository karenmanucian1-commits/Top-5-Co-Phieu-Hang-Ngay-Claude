"""
Ghi lại TOP5 mỗi ngày và chấm hiệu quả thật sau 5 / 20 phiên.
Đây là cách duy nhất biết bot có thật sự hiệu quả hay không.
"""
import os

import pandas as pd

import config

LOG_PATH = os.path.join(config.DATA_DIR, "picks_log.csv")
COLS = ["run_date", "session", "rank", "symbol", "price", "entry", "stop", "t1",
        "status", "priority", "final_score"]


def log_picks(rows):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    new = pd.DataFrame(rows, columns=COLS)
    if os.path.exists(LOG_PATH):
        old = pd.read_csv(LOG_PATH)
        # chạy lại cùng ngày cùng phiên thì ghi đè
        key = (old["run_date"].astype(str) == str(new["run_date"].iloc[0])) & \
              (old["session"] == new["session"].iloc[0])
        old = old[~key]
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(LOG_PATH, index=False)


def evaluate(price_map, idx_df, lookbacks=(5, 20)):
    """So sánh giá hiện tại với giá lúc bot chọn (chỉ dùng lượt CHIỀU = giá đóng cửa)."""
    if not os.path.exists(LOG_PATH):
        return []
    log = pd.read_csv(LOG_PATH)
    log = log[log["session"] == "CHIEU"]
    if log.empty:
        return []
    dates = [d.strftime("%Y-%m-%d") for d in pd.to_datetime(idx_df["date"])]
    out = []
    for n in lookbacks:
        if len(dates) <= n:
            continue
        d0 = dates[-1 - n]
        picks = log[log["run_date"].astype(str) == d0]
        if picks.empty:
            continue
        rets = []
        best = None
        for _, r in picks.iterrows():
            df = price_map.get(r["symbol"])
            if df is None or not r["price"]:
                continue
            ret = (float(df["close"].iloc[-1]) / float(r["price"]) - 1) * 100
            rets.append(ret)
            if int(r["priority"]) == 1:
                best = (r["symbol"], ret)
        if not rets:
            continue
        i0 = float(idx_df["close"].iloc[-1 - n])
        idx_ret = (float(idx_df["close"].iloc[-1]) / i0 - 1) * 100
        out.append({
            "n": n, "date": d0, "avg": sum(rets) / len(rets), "idx": idx_ret,
            "win": sum(1 for x in rets if x > 0), "total": len(rets), "priority": best,
        })
    return out
