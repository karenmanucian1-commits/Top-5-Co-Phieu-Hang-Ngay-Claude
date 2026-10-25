"""
BACKTEST CLAUDE BOT V1 – cho bot "sống lại" từng ngày trong quá khứ.

  py backtest.py                 # mặc định mô phỏng từ 01/01/2023
  py backtest.py --start 2024-01-01
  py backtest.py --refresh       # tải lại toàn bộ dữ liệu

NGUYÊN TẮC (đóng băng V1):
  - Dùng NGUYÊN các hàm chấm điểm của V1 trong analysis.py + config.py, không sửa ngưỡng.
  - Mỗi ngày bot chỉ thấy dữ liệu đến hết ngày đó (giá đóng cửa = lượt 6:00 sáng hôm sau).
  - BCTC quý chỉ được dùng sau 45 ngày kể từ cuối quý (thời hạn công bố).
  - Vào lệnh bằng GIÁ MỞ CỬA phiên kế tiếp, trừ phí 0.4%.
  - Mô phỏng vùng giá: theo T+2.5, chỉ được bán từ phiên thứ 3; chạm cắt lỗ & chốt lời cùng ngày
    -> tính là cắt lỗ (thận trọng).
HẠN CHẾ: thiếu mã đã huỷ niêm yết (kết quả hơi đẹp hơn thực tế); chưa xử lý cổ tức/chia tách.
"""
import argparse
import json
import logging
import os
import pickle
import time
from datetime import timedelta

import numpy as np
import pandas as pd

import analysis as an
import config
import indicators as ind
import vci_client as vci

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bt")

D = config.DATA_DIR
PRICE_CACHE = os.path.join(D, "bt_prices.pkl")
RATIO_CACHE = os.path.join(D, "bt_ratios.json")
HORIZONS = (5, 10, 20)
FUND_LAG_DAYS = 45


# =============================================================================
# DỮ LIỆU
# =============================================================================
def load_prices(bars, refresh, client=vci):
    if os.path.exists(PRICE_CACHE) and not refresh:
        with open(PRICE_CACHE, "rb") as fh:
            data = pickle.load(fh)
        log.info("Dùng giá đã lưu: %d mã", len(data["prices"]))
        return data
    syms = client.get_all_symbols()
    idx = client.get_ohlc([config.INDEX_SYMBOL], bars=bars)[config.INDEX_SYMBOL]
    log.info("Tải %d phiên giá cho %d mã (lần đầu ~15-25 phút)...", bars, len(syms))
    prices = client.get_ohlc(syms["symbol"].tolist(), bars=bars)
    data = {"symbols": syms, "index": idx, "prices": prices,
            "industries": client.get_industries()}
    os.makedirs(D, exist_ok=True)
    with open(PRICE_CACHE, "wb") as fh:
        pickle.dump(data, fh)
    return data


class Ratios:
    """Chỉ số tài chính theo quý, chỉ trả về những quý ĐÃ công bố tại ngày mô phỏng."""

    def __init__(self, client=vci):
        self.client = client
        self.raw = {}
        self.memo = {}
        self.dirty = 0
        if os.path.exists(RATIO_CACHE):
            with open(RATIO_CACHE, encoding="utf-8") as fh:
                self.raw = json.load(fh)

    def save(self):
        if self.dirty:
            with open(RATIO_CACHE, "w", encoding="utf-8") as fh:
                json.dump(self.raw, fh, ensure_ascii=False)
            self.dirty = 0

    def _recs(self, sym):
        if sym not in self.raw:
            try:
                self.raw[sym] = self.client.get_ratios(sym)
            except Exception as e:  # noqa: BLE001
                log.warning("Không lấy được BCTC %s: %s", sym, e)
                self.raw[sym] = []
            self.dirty += 1
            time.sleep(config.REQUEST_SLEEP)
            if self.dirty >= 25:
                self.save()
        return self.raw[sym]

    def at(self, sym, date):
        avail = []
        for r in self._recs(sym):
            try:
                y, q = int(r.get("year") or r.get("yearReport")), int(r.get("quarter") or r.get("lengthReport"))
            except (TypeError, ValueError):
                continue
            if not 1 <= q <= 4:
                continue
            q_end = pd.Timestamp(year=y, month=3 * q, day=1) + pd.offsets.MonthEnd(0)
            if q_end + timedelta(days=FUND_LAG_DAYS) <= date:
                avail.append(r)
        key = (sym, len(avail))
        if key not in self.memo:
            try:
                self.memo[key] = an.parse_ratios(avail) if avail else None
            except Exception:  # noqa: BLE001
                self.memo[key] = None
        return self.memo[key]


# =============================================================================
# ĐẶC TRƯNG KỸ THUẬT – bản tính 1 lần cho cả chuỗi (giống hệt technical_features của V1)
# =============================================================================
def feature_frame(df):
    c, v = df["close"], df["volume"].astype(float)
    val = c * 1000 * v
    ma50 = ind.sma(c, 50)
    _, _, hist = ind.macd(c)
    adx14, pdi, mdi = ind.adx(df, 14)
    chg = c.diff()
    up = v.where(chg > 0, 0.0).rolling(20).sum()
    down = v.where(chg < 0, 0.0).rolling(20).sum()
    vol20 = v.rolling(20).mean()
    out = pd.DataFrame({
        "date": df["date"].values,
        "price": c.values,
        "bars": np.arange(1, len(df) + 1),
        "value20": val.rolling(20).mean().values,
        "median_value20": val.rolling(20).median().values,
        "trade_days": (v > 0).astype(int).rolling(20).sum().values,
        "ma20": ind.sma(c, 20).values,
        "ma50": ma50.values,
        "ma200": ind.sma(c, 200).values,
        "ma50_prev": ma50.shift(config.MA50_SLOPE_LOOKBACK).values,
        "rsi": ind.rsi(c, 14).values,
        "macd_hist": hist.values,
        "macd_hist_prev": hist.shift(3).values,
        "adx": adx14.values, "pdi": pdi.values, "mdi": mdi.values,
        "atr": ind.atr(df, config.ATR_PERIOD).values,
        "ret5": (c / c.shift(5) - 1).values,
        "ret20": (c / c.shift(20) - 1).values,
        "ret60": (c / c.shift(60) - 1).values,
        "high252": df["high"].rolling(252, min_periods=1).max().values,
        "vol_ratio": (v.rolling(5).mean() / vol20.replace(0, np.nan)).values,
        "updown_vol": np.where(down > 0, up / down.replace(0, np.nan), 3.0),
    })
    return out


def row_to_f(r):
    f = r.to_dict()
    f["last_date"] = r["date"]
    if pd.isna(f["ma200"]):
        f["ma200"] = None
    for k in ("ret5", "ret20", "ret60", "vol_ratio"):
        if pd.isna(f[k]):
            f[k] = None
    return f


# =============================================================================
# KẾT QUẢ SAU KHI CHỌN
# =============================================================================
def outcomes(df, p, z):
    """p = vị trí phiên tín hiệu. Vào lệnh giá mở cửa phiên p+1."""
    o = {}
    e = p + 1
    if e >= len(df):
        return None
    entry = float(df["open"].iloc[e]) or float(df["close"].iloc[e])
    fee = config.FEE_ROUNDTRIP
    for h in HORIZONS:
        j = p + h
        o[f"ret{h}"] = (float(df["close"].iloc[j]) / entry - 1 - fee) * 100 if j < len(df) else np.nan
    # Mô phỏng theo vùng giá: cắt lỗ / chốt lời mục tiêu 1 / hết 20 phiên
    stop, t1 = z["stop"], z["t1"]
    exit_px, how, held = None, None, None
    for j in range(e, min(e + 20, len(df))):
        if j < e + 2:  # T+2.5: chưa bán được
            continue
        op, lo, hi = (float(df[k].iloc[j]) for k in ("open", "low", "high"))
        if lo <= stop:
            exit_px, how = (op if op < stop else stop), "STOP"
        elif hi >= t1:
            exit_px, how = (op if op > t1 else t1), "T1"
        if how:
            held = j - e + 1
            break
    if how is None:
        j = min(e + 19, len(df) - 1)
        if j - e < 19:
            o.update(trade_ret=np.nan, exit="CHUA_DU", held=np.nan)
            return o
        exit_px, how, held = float(df["close"].iloc[j]), "HET_20_PHIEN", 20
    o.update(trade_ret=(exit_px / entry - 1 - fee) * 100, exit=how, held=held)
    return o


# =============================================================================
# MÔ PHỎNG
# =============================================================================
def fwd(df, p):
    """Lợi nhuận đơn giản: mua giá mở cửa phiên sau, bán giá đóng cửa sau h phiên (đã trừ phí)."""
    e = p + 1
    if e >= len(df):
        return {}
    entry = float(df["open"].iloc[e]) or float(df["close"].iloc[e])
    return {f"ret{h}": (float(df["close"].iloc[p + h]) / entry - 1 - config.FEE_ROUNDTRIP) * 100
            for h in HORIZONS if p + h < len(df)}


def run(start, bars, refresh, client=vci, panel_out=False):
    data = load_prices(bars, refresh, client)
    idx = data["index"].reset_index(drop=True)
    prices, inds = data["prices"], data["industries"]
    meta = data["symbols"].set_index("symbol").to_dict("index")
    ratios = Ratios(client)

    log.info("Tính chỉ báo cho từng mã...")
    feats, raw = {}, {}
    for s, df in prices.items():
        if len(df) < config.MIN_BARS + 20:
            continue
        df = df.reset_index(drop=True)
        ff = feature_frame(df)
        if np.nanmax(ff["value20"].values) < config.MIN_VALUE20_VND:
            continue
        feats[s] = ff.set_index("date")
        raw[s] = df
    log.info("%d mã từng đạt GTGD ≥3 tỷ trong giai đoạn", len(feats))

    panel = pd.concat({s: f for s, f in feats.items()}, names=["symbol", "date"])
    panel = panel.reset_index()
    panel = panel[panel["value20"] >= config.MIN_VALUE20_VND]
    by_date = dict(tuple(panel.groupby("date")))

    idx_c = idx["close"]
    sim_dates = [d for d in idx["date"] if d >= pd.Timestamp(start)]
    pos_cache = {s: pd.Index(raw[s]["date"]) for s in raw}
    rows_out, panel_rows = [], []

    for k, d in enumerate(sim_dates):
        ip = int(idx.index[idx["date"] == d][0])
        if ip < 210:
            continue
        ir20 = float(idx_c.iloc[ip] / idx_c.iloc[ip - 20] - 1)
        ir60 = float(idx_c.iloc[ip] / idx_c.iloc[ip - 60] - 1)

        idx_out = {}
        if ip + 1 < len(idx):
            i_entry = float(idx["open"].iloc[ip + 1]) or float(idx_c.iloc[ip + 1])
            for h in HORIZONS:
                idx_out[f"idx{h}"] = (float(idx_c.iloc[ip + h]) / i_entry - 1) * 100 if ip + h < len(idx) else np.nan

        day = by_date.get(d)
        if day is None:
            continue
        liquid = {}
        for _, r in day.iterrows():
            if r["bars"] < config.MIN_BARS:
                continue
            f = row_to_f(r)
            if an.liquidity_filter(f, d):
                continue
            f["rs20"] = f["ret20"] - ir20 if f["ret20"] is not None else None
            f["rs60"] = f["ret60"] - ir60 if f["ret60"] is not None else None
            liquid[r["symbol"]] = f
        if not liquid:
            continue

        rs = pd.DataFrame({s: {"rs20": f["rs20"], "rs60": f["rs60"]} for s, f in liquid.items()}).T.astype(float)
        p20, p60 = rs["rs20"].rank(pct=True), rs["rs60"].rank(pct=True)
        for s, f in liquid.items():
            f["rs_pct20"] = float(p20[s]) if pd.notna(p20[s]) else 0.5
            f["rs_pct60"] = float(p60[s]) if pd.notna(p60[s]) else 0.5
        breadth = sum(f["price"] > f["ma50"] for f in liquid.values()) / len(liquid) * 100
        market = an.market_regime(idx.iloc[: ip + 1], breadth)

        trend = {}
        for s, f in liquid.items():
            f["tech"], f["tech_d"] = an.technical_score(f)
            f["trend_ok"] = an.trend_filter(f) is None
            if not f["trend_ok"]:
                continue
            trend[s] = f
        shortlist = sorted(trend, key=lambda s: trend[s]["tech"], reverse=True)[: config.FUND_CANDIDATES]

        scored = []
        for s in shortlist:
            f = trend[s]
            com_type = inds.get(s, {}).get("com_type", "CT")
            fd = ratios.at(s, d)
            if an.fundamental_filter(fd):
                continue
            fund, _ = an.fundamental_score(fd, com_type)
            p = pos_cache[s].get_loc(f["last_date"])
            hist_df = raw[s].iloc[: p + 1]
            z = an.compute_zones(hist_df, f)
            setup = an.setup_score(z, f)
            final = config.W_TECH * f["tech"] + config.W_FUND * fund + config.W_SETUP * setup
            scored.append({"symbol": s, "sector": inds.get(s, {}).get("sector", "Khác"),
                           "f": f, "fd": fd, "z": z, "tech": f["tech"], "fund": fund,
                           "setup": setup, "final": final, "p": p})

        if panel_out:
            # Ghi lại TOÀN BỘ mã thanh khoản tốt trong ngày (không chỉ TOP5) để phân tích từng yếu tố
            for s, f in liquid.items():
                p = pos_cache[s].get_loc(f["last_date"])
                fd = ratios.at(s, d)
                com_type = inds.get(s, {}).get("com_type", "CT")
                fund, _ = an.fundamental_score(fd, com_type)
                td = f["tech_d"]
                panel_rows.append({
                    "date": d.date(), "symbol": s, "sector": inds.get(s, {}).get("sector", "Khác"),
                    "exchange": meta.get(s, {}).get("exchange", ""), "com_type": com_type,
                    "mkt_points": market["points"], "breadth": round(breadth, 1),
                    "trend_ok": int(f["trend_ok"]), "in_top5": 0,
                    "price": f["price"], "value20": f["value20"] / 1e9,
                    "dist_ma20": f["price"] / f["ma20"] - 1, "dist_ma50": f["price"] / f["ma50"] - 1,
                    "dist_ma200": (f["price"] / f["ma200"] - 1) if f["ma200"] else np.nan,
                    "dist_high252": f["price"] / f["high252"] - 1,
                    "ret5": f["ret5"], "ret20": f["ret20"], "ret60": f["ret60"],
                    "rs20": f["rs20"], "rs60": f["rs60"], "rs_pct20": f["rs_pct20"], "rs_pct60": f["rs_pct60"],
                    "rsi": f["rsi"], "adx": f["adx"], "di_diff": f["pdi"] - f["mdi"], "macd_hist_pct": f["macd_hist"] / f["price"],
                    "atr_pct": f["atr"] / f["price"], "vol_ratio": f["vol_ratio"], "updown_vol": f["updown_vol"],
                    "s_trend": td["trend"], "s_rs": td["rs"], "s_momentum": td["momentum"], "s_flow": td["flow"],
                    "s_adx": td["adx"], "tech": f["tech"], "fund": fund,
                    "roe": fd["roe"] if fd else np.nan, "growth": fd["eps_growth"] if fd else np.nan,
                    "pe": fd["pe"] if fd else np.nan, "pb": fd["pb"] if fd else np.nan,
                    "de": fd["de"] if fd else np.nan, "margin": fd["margin"] if fd else np.nan,
                    **{f"f{k}": v for k, v in fwd(raw[s], p).items()},
                    **{f"idx{h}": idx_out.get(f"idx{h}", np.nan) for h in HORIZONS},
                })

        scored.sort(key=lambda x: x["final"], reverse=True)
        top, per = [], {}
        for x in scored:
            if per.get(x["sector"], 0) >= config.MAX_PER_SECTOR:
                continue
            top.append(x)
            per[x["sector"]] = per.get(x["sector"], 0) + 1
            if len(top) >= config.TOP_N:
                break
        buyable = [x for x in top if x["z"]["status"] == "MUA" and x["fd"] is not None]
        prio = max(buyable, key=lambda x: x["final"]) if buyable else None
        if prio and market["points"] <= 1 and prio["setup"] < 70:
            prio = None


        for rank, x in enumerate(top, 1):
            o = outcomes(raw[x["symbol"]], x["p"], x["z"])
            if o is None:
                continue
            rows_out.append({
                "date": d.date(), "rank": rank, "symbol": x["symbol"], "exchange": meta.get(x["symbol"], {}).get("exchange", ""),
                "sector": x["sector"], "status": x["z"]["status"],
                "priority": int(prio is not None and x["symbol"] == prio["symbol"]),
                "final": round(x["final"], 1), "tech": round(x["tech"], 1), "fund": round(x["fund"], 1),
                "setup": round(x["setup"], 1), "regime": market["regime"], "price": round(x["f"]["price"], 2),
                "stop": round(x["z"]["stop"], 2), "t1": round(x["z"]["t1"], 2), "rr1": round(x["z"]["rr1"], 2),
                **o, **idx_out,
            })
        if k % 20 == 0:
            log.info("Đã mô phỏng %s (%d/%d ngày) – %d lượt chọn", d.date(), k + 1, len(sim_dates), len(rows_out))
            ratios.save()

    ratios.save()
    if panel_out:
        pn = pd.DataFrame(panel_rows)
        top_keys = {(r["date"], r["symbol"]) for r in rows_out}
        pn["in_top5"] = [int((a, b) in top_keys) for a, b in zip(pn["date"], pn["symbol"])]
        path = os.path.join(D, "bt_panel.csv.gz")
        pn.to_csv(path, index=False, compression="gzip")
        print(f"Đã lưu dữ liệu toàn thị trường: {path} ({len(pn):,} dòng)")
    res = pd.DataFrame(rows_out)
    out_csv = os.path.join(D, "bt_picks_v1.csv")
    res.to_csv(out_csv, index=False, encoding="utf-8-sig")
    summary = summarize(res)
    with open(os.path.join(D, "bt_summary_v1.txt"), "w", encoding="utf-8") as fh:
        fh.write(summary)
    print(summary)
    print(f"\nChi tiết từng lượt chọn: {out_csv}")
    return res


# =============================================================================
# TỔNG KẾT
# =============================================================================
def _block(df, title):
    lines = [f"\n■ {title}  (n = {len(df)})"]
    if df.empty:
        return lines + ["  (không có dữ liệu)"]
    for h in HORIZONS:
        r, i = df[f"ret{h}"].dropna(), df.loc[df[f"ret{h}"].notna(), f"idx{h}"]
        if r.empty:
            continue
        alpha = (r - i.values).mean()
        lines.append(f"  Sau {h:>2} phiên: TB {r.mean():+5.2f}% | trung vị {r.median():+5.2f}% | "
                     f"thắng {(r > 0).mean() * 100:4.0f}% | VNI {i.mean():+5.2f}% | ALPHA {alpha:+5.2f}%")
    return lines


def summarize(res):
    if res.empty:
        return "Không có lượt chọn nào."
    L = ["=" * 78, "KẾT QUẢ BACKTEST CLAUDE BOT V1",
         f"Giai đoạn: {res['date'].min()} → {res['date'].max()} | {res['date'].nunique()} phiên | {len(res)} lượt chọn",
         "Lợi nhuận đã trừ phí 0.4%, vào lệnh giá mở cửa phiên sau. ALPHA = hơn/kém VN-Index.",
         "=" * 78]
    L += _block(res, "TẤT CẢ TOP5")
    L += _block(res[res["priority"] == 1], "⭐ ƯU TIÊN #1")
    L += _block(res[res["status"] == "MUA"], "Mã trạng thái MUA ĐƯỢC")
    L += _block(res[res["status"] != "MUA"], "Mã KHÔNG ở điểm mua (chờ/không đuổi)")

    L.append("\n■ THEO THỨ HẠNG (hạng 1 có thật sự tốt hơn hạng 5?)")
    for rk, g in res.groupby("rank"):
        r = g["ret20"].dropna()
        a = (r - g.loc[r.index, "idx20"]).mean()
        L.append(f"  Hạng {rk}: TB 20 phiên {r.mean():+5.2f}% | thắng {(r > 0).mean() * 100:4.0f}% | alpha {a:+5.2f}% | n={len(r)}")

    t = res[(res["status"] == "MUA") & res["trade_ret"].notna()]
    L.append(f"\n■ LÀM ĐÚNG THEO VÙNG GIÁ (mã MUA ĐƯỢC, n = {len(t)})")
    if len(t):
        for how, g in t.groupby("exit"):
            L.append(f"  {how:<13}: {len(g) / len(t) * 100:4.0f}% số lệnh | TB {g['trade_ret'].mean():+5.2f}% | giữ TB {g['held'].mean():.1f} phiên")
        L.append(f"  => TB mỗi lệnh {t['trade_ret'].mean():+5.2f}% | thắng {(t['trade_ret'] > 0).mean() * 100:.0f}%")

    L.append("\n■ THEO THỊ TRƯỜNG (20 phiên)")
    for reg, g in res.groupby("regime"):
        r = g["ret20"].dropna()
        a = (r - g.loc[r.index, "idx20"]).mean()
        L.append(f"  {reg:<14}: TB {r.mean():+5.2f}% | alpha {a:+5.2f}% | n={len(r)}")

    L.append("\n■ THEO NĂM (20 phiên)")
    yr = pd.to_datetime(res["date"]).dt.year
    for y, g in res.groupby(yr):
        r = g["ret20"].dropna()
        a = (r - g.loc[r.index, "idx20"]).mean()
        L.append(f"  {y}: TB {r.mean():+5.2f}% | alpha {a:+5.2f}% | thắng {(r > 0).mean() * 100:4.0f}% | n={len(r)}")
    L.append("\nLưu ý: các lượt chọn liên tiếp chồng lên nhau nên số mẫu 'độc lập' ít hơn n.")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--bars", type=int, default=1100, help="số phiên lịch sử tải về (~4.4 năm)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--panel", action="store_true", help="xuất dữ liệu toàn bộ mã thanh khoản để phân tích yếu tố")
    a = ap.parse_args()
    run(a.start, a.bars, a.refresh, panel_out=a.panel)
