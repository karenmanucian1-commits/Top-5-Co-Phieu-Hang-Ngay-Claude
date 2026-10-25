"""
NGHIÊN CỨU CLAUDE V3 – LƯỚT SÓNG, MỤC TIÊU LÃI ≥ 2%/LỆNH SAU PHÍ.

Chạy trên GitHub Actions (workflow RESEARCH V3). Không đụng bot V1/V2 đang chạy.

LUẬT ĐÃ KHÓA TRƯỚC KHI CHẠY (không chỉnh theo Holdout):
  - Development: 01/01/2023 → 23/04/2026. Holdout: 24/04/2026 → hết dữ liệu.
  - Mua giá MỞ CỬA phiên sau tín hiệu. Phí mua + bán 0.4%.
  - T+2.5: chỉ bán được từ phiên thứ 3 (j >= e+2).
  - Mỗi phiên: Close < cắt lỗ -> bán Open phiên kế tiếp (xét TRƯỚC);
               High >= chốt lời -> bán tại max(chốt lời, Open phiên đó);
               hết H phiên -> bán Close phiên thứ H.
  - Một mã đang giữ thì không mua lại cho tới khi hết H phiên.
  - Chỉ số tài chính dùng sau 45 ngày kể từ cuối quý.
  - Chọn luật: trên Development, trong các luật có chốt lời >= +2.5% (lãi ròng >= ~2%), H <= 10 phiên:
        tỷ lệ thắng >= 55%, lãi TB > 0 ở CẢ 2023, 2024, 2025 riêng lẻ, số lệnh >= 200
        -> lấy luật có LÃI TB/LỆNH cao nhất.
    Sau đó báo kết quả luật đó trên Holdout + so VNINDEX cùng khoảng thời gian. Không chọn lại.
"""
import json
import logging
import os
import pickle
import time

import numpy as np
import pandas as pd

import analysis as an
import config
import vci_client as vci
from backtest import Ratios, feature_frame, row_to_f

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("v3")

D = config.DATA_DIR
OUT = os.path.join(D, "research")
PRICE_CACHE = os.path.join(D, "bt_prices.pkl")
FEE = config.FEE_ROUNDTRIP
DEV_END = pd.Timestamp("2026-04-23")
START = pd.Timestamp("2023-01-01")
BARS = 1100

TARGETS = [0.025, 0.03, 0.035, 0.04, 0.05, 0.06]
STOPS = [0.03, 0.05, 0.07, 0.10, None]
HORIZONS = [5, 10, 15]


# ----------------------------------------------------------------------------- dữ liệu
def load_data():
    if os.path.exists(PRICE_CACHE):
        with open(PRICE_CACHE, "rb") as fh:
            data = pickle.load(fh)
        log.info("Dùng giá đã lưu: %d mã", len(data["prices"]))
        return data
    syms = vci.get_all_symbols()
    idx = vci.get_ohlc([config.INDEX_SYMBOL], bars=BARS)[config.INDEX_SYMBOL]
    log.info("Tải %d phiên cho %d mã...", BARS, len(syms))
    prices = vci.get_ohlc(syms["symbol"].tolist(), bars=BARS, max_minutes=100)
    data = {"symbols": syms, "index": idx, "prices": prices, "industries": vci.get_industries()}
    os.makedirs(D, exist_ok=True)
    with open(PRICE_CACHE, "wb") as fh:
        pickle.dump(data, fh)
    return data


# ----------------------------------------------------------------------------- mô phỏng
def path(df, p, hmax):
    """Mảng giá từ phiên mua e=p+1 tới e+hmax (cần thêm 1 phiên để bán Open sau khi thủng cắt lỗ)."""
    e = p + 1
    if e + hmax >= len(df):
        return None
    o, h, c = df["open"].values, df["high"].values, df["close"].values
    entry = float(o[e]) or float(c[e])
    sl = slice(e, e + hmax + 1)
    return entry, o[sl], h[sl], c[sl]


def simulate(pth, tgt, stop, H):
    entry, o, h, c = pth
    t_px = entry * (1 + tgt)
    s_px = entry * (1 - stop) if stop else None
    for j in range(2, H):
        if s_px is not None and c[j] < s_px:
            return (o[j + 1] / entry - 1 - FEE) * 100, j + 1, "STOP"
        if h[j] >= t_px:
            return (max(t_px, o[j]) / entry - 1 - FEE) * 100, j, "TP"
    return (c[H - 1] / entry - 1 - FEE) * 100, H - 1, "TIME"


# ----------------------------------------------------------------------------- tín hiệu
def build_signals(data):
    idx = data["index"].reset_index(drop=True)
    idx["date"] = pd.to_datetime(idx["date"])
    prices = data["prices"]
    ratios = Ratios()

    feats, raw = {}, {}
    for s, df in prices.items():
        if len(df) < config.MIN_BARS + 20:
            continue
        df = df.reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        ff = feature_frame(df)
        if np.nanmax(ff["value20"].values) < config.MIN_VALUE20_VND:
            continue
        feats[s] = ff.set_index("date")
        raw[s] = df
    log.info("%d mã từng đạt GTGD ≥ 3 tỷ", len(feats))
    panel = pd.concat(feats, names=["symbol", "date"]).reset_index()
    panel = panel[panel["value20"] >= config.MIN_VALUE20_VND]
    by_date = dict(tuple(panel.groupby("date")))
    pos = {s: pd.Index(raw[s]["date"]) for s in raw}

    ic = idx["close"]
    idx_ma50 = ic.rolling(50).mean()
    idx_up = dict(zip(idx["date"], ic > idx_ma50))

    sig = []
    days = [d for d in idx["date"] if d >= START]
    for k, d in enumerate(days):
        day = by_date.get(d)
        if day is None:
            continue
        rows = []
        for _, r in day.iterrows():
            if r["bars"] < config.MIN_BARS:
                continue
            f = row_to_f(r)
            if an.liquidity_filter(f, d):
                continue
            fd = ratios.at(r["symbol"], d)
            ok_fd = bool(fd) and all(fd.get(x) is not None for x in ("margin", "roe", "pb"))
            rows.append({
                "symbol": r["symbol"], "p": pos[r["symbol"]].get_loc(d),
                "margin": fd["margin"] if ok_fd else np.nan, "roe": fd["roe"] if ok_fd else np.nan,
                "pb": fd["pb"] if ok_fd else np.nan,
                "atr_pct": f["atr"] / f["price"], "adx": f["adx"], "rsi": f["rsi"],
                "trend": f["price"] > f["ma50"] and f["rsi"] >= 50,
                "pullback": f["price"] <= f["ma20"] + 0.5 * f["atr"],
                "rsi_ok": 50 <= f["rsi"] <= 65,
            })
        if len(rows) < 20:
            continue
        t = pd.DataFrame(rows)
        q = t.dropna(subset=["margin", "roe", "pb", "atr_pct", "adx"]).copy()
        q["score"] = (q["margin"].rank(pct=True) + q["roe"].rank(pct=True) + q["pb"].rank(pct=True)
                      + (1 - q["atr_pct"].rank(pct=True)) + q["adx"].rank(pct=True)) / 5
        q["rank"] = q["score"].rank(ascending=False)
        t = t.merge(q[["symbol", "score", "rank"]], on="symbol", how="left")
        t["date"] = d
        t["mkt_up"] = bool(idx_up.get(d, False))
        sig.append(t)
        if k % 100 == 0:
            log.info("Đã xử lý tới %s", d.date())
            ratios.save()
    ratios.save()
    return pd.concat(sig, ignore_index=True), raw, idx


SETS = {
    "S0 Tất cả mã thanh khoản (mốc so sánh)": lambda t: pd.Series(True, index=t.index),
    "S1 Chỉ xu hướng (giá>MA50, RSI≥50)": lambda t: t["trend"],
    "S2 V2: top30 chất lượng + xu hướng": lambda t: t["trend"] & (t["rank"] <= 30),
    "S3 V2 + mua nhịp chỉnh (≤MA20+0.5ATR)": lambda t: t["trend"] & (t["rank"] <= 30) & t["pullback"],
    "S4 V2 + RSI 50–65 (chưa nóng)": lambda t: t["trend"] & (t["rank"] <= 30) & t["rsi_ok"],
    "S5 V2 + VNINDEX trên MA50": lambda t: t["trend"] & (t["rank"] <= 30) & t["mkt_up"],
    "S6 V2 + nhịp chỉnh + VNINDEX trên MA50": lambda t: t["trend"] & (t["rank"] <= 30) & t["pullback"] & t["mkt_up"],
}


def dedupe(s, H):
    """Một mã đang giữ (H phiên) thì bỏ tín hiệu mới."""
    s = s.sort_values(["symbol", "p"])
    keep, last = [], {}
    for i, sym, p in zip(s.index, s["symbol"], s["p"]):
        if sym in last and p - last[sym] < H:
            continue
        last[sym] = p
        keep.append(i)
    return s.loc[keep]


def main():
    os.makedirs(OUT, exist_ok=True)
    data = load_data()
    sig, raw, idx = build_signals(data)
    log.info("Tổng %d dòng mã-ngày", len(sig))
    ipos = pd.Index(idx["date"])
    io, icl = idx["open"].values, idx["close"].values

    rows = []
    for sname, fn in SETS.items():
        base = sig[fn(sig).fillna(False).astype(bool)]
        for H in HORIZONS:
            s = dedupe(base, H)
            paths = []
            for r in s.itertuples():
                pth = path(raw[r.symbol], r.p, H)
                if pth is None:
                    continue
                ip = ipos.get_loc(r.date)
                if ip + H >= len(icl):
                    continue
                paths.append((r.date, pth, ip))
            if not paths:
                continue
            dates = pd.to_datetime([x[0] for x in paths])
            per = np.where(dates <= DEV_END, "DEV", "HOLDOUT")
            years = dates.year
            for tgt in TARGETS:
                for stop in STOPS:
                    res = [simulate(x[1], tgt, stop, H) for x in paths]
                    ret = np.array([x[0] for x in res])
                    held = np.array([x[1] for x in res])
                    how = np.array([x[2] for x in res])
                    vni = np.array([(icl[x[2] + 1 + hd] / io[x[2] + 1] - 1 - FEE) * 100
                                    for x, hd in zip(paths, held)])
                    for pname in ("DEV", "HOLDOUT"):
                        m = per == pname
                        if m.sum() < 20:
                            continue
                        a = ret[m] - vni[m]
                        row = {"set": sname, "H": H, "tp": tgt * 100, "sl": stop * 100 if stop else None,
                               "period": pname, "n": int(m.sum()), "ev": ret[m].mean(), "median": np.median(ret[m]),
                               "win": (ret[m] > 0).mean() * 100, "ge2": (ret[m] >= 2).mean() * 100,
                               "tp_rate": (how[m] == "TP").mean() * 100, "stop_rate": (how[m] == "STOP").mean() * 100,
                               "hold": held[m].mean() + 1, "vni": vni[m].mean(), "alpha": a.mean(),
                               "alpha_t": a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 2 else np.nan}
                        if pname == "DEV":
                            for y in (2023, 2024, 2025):
                                my = m & (years == y)
                                row[f"ev{y}"] = ret[my].mean() if my.sum() else np.nan
                        rows.append(row)
            log.info("Xong %s H=%d (%d lệnh)", sname, H, len(paths))

    r = pd.DataFrame(rows)
    r.to_csv(os.path.join(OUT, "v3_grid.csv"), index=False, encoding="utf-8-sig")

    dev = r[r["period"] == "DEV"].copy()
    elig = dev[(dev["tp"] >= 2.5) & (dev["H"] <= 10) & (dev["win"] >= 55) & (dev["n"] >= 200)
               & (dev[["ev2023", "ev2024", "ev2025"]] > 0).all(axis=1) & (~dev["set"].str.startswith("S0"))]
    elig = elig.sort_values("ev", ascending=False)
    hold = r[r["period"] == "HOLDOUT"].set_index(["set", "H", "tp", "sl"])

    def fmt(x, cols):
        return x[cols].round(2).to_string(index=False)

    cols = ["set", "H", "tp", "sl", "n", "ev", "median", "win", "ge2", "tp_rate", "stop_rate", "hold", "vni", "alpha", "alpha_t"]
    L = ["=" * 110, "NGHIÊN CỨU CLAUDE V3 – LƯỚT SÓNG, MỤC TIÊU ≥ 2%/LỆNH (đã trừ phí 0.4%)", "=" * 110,
         f"Development 2023 → {DEV_END.date()} | Holdout sau đó tới {sig['date'].max().date()}",
         "ev = lãi TB/lệnh %, win = % lệnh có lãi, ge2 = % lệnh lãi ≥ 2%, tp_rate = % chạm chốt lời, "
         "hold = số phiên giữ TB, vni = VNINDEX cùng khoảng, alpha = hơn VNINDEX, alpha_t = độ tin cậy (>2 là có ý nghĩa)", ""]
    L.append("■ MỐC SO SÁNH – TẤT CẢ MÃ THANH KHOẢN (DEV)")
    L.append(fmt(dev[dev["set"].str.startswith("S0")].sort_values("ev", ascending=False).head(5), cols))
    L.append("\n■ TOP 15 LUẬT ĐẠT ĐIỀU KIỆN TRÊN DEVELOPMENT (thắng ≥55%, lãi >0 cả 3 năm, H ≤ 10, chốt ≥ +2.5%)")
    L.append(fmt(elig.head(15), cols + ["ev2023", "ev2024", "ev2025"]) if len(elig) else "KHÔNG CÓ LUẬT NÀO ĐẠT")
    best = None
    if len(elig):
        best = elig.iloc[0]
        key = (best["set"], best["H"], best["tp"], best["sl"])
        L.append("\n■ LUẬT ĐƯỢC CHỌN (khóa từ Development):")
        L.append(f"  {best['set']} | giữ tối đa {int(best['H'])} phiên | chốt +{best['tp']}% | "
                 f"cắt lỗ {('-' + str(best['sl']) + '%') if pd.notna(best['sl']) else 'không'}")
        L.append("  DEV    : " + ", ".join(f"{c}={best[c]:.2f}" for c in cols[4:]))
        if key in hold.index:
            h = hold.loc[key]
            L.append("  HOLDOUT: " + ", ".join(f"{c}={h[c]:.2f}" for c in cols[4:]))
            s0 = r[(r["set"].str.startswith("S0")) & (r["H"] == best["H"]) & (r["tp"] == best["tp"])
                   & ((r["sl"] == best["sl"]) | (r["sl"].isna() & pd.isna(best["sl"])))].set_index("period")
            for pn in ("DEV", "HOLDOUT"):
                if pn in s0.index:
                    L.append(f"  Mốc S0 cùng luật {pn}: ev={s0.loc[pn, 'ev']:.2f}, win={s0.loc[pn, 'win']:.2f}, alpha={s0.loc[pn, 'alpha']:.2f}")
            beat0 = all(pn in s0.index and (best["ev"] if pn == "DEV" else h["ev"]) > s0.loc[pn, "ev"] for pn in ("DEV", "HOLDOUT"))
            verdict = "ĐẠT" if h["ev"] > 0 and h["alpha"] > 0 and beat0 else "KHÔNG ĐẠT"
            L.append(f"  ➜ Holdout: {verdict} (cần lãi TB > 0, hơn VNINDEX, và hơn mốc S0 cùng luật ở cả DEV lẫn HOLDOUT)")
        L.append("\n■ 10 LUẬT ĐẦU TRÊN HOLDOUT (chỉ để xem, KHÔNG dùng để chọn lại):")
        hh = []
        for _, e in elig.head(10).iterrows():
            k = (e["set"], e["H"], e["tp"], e["sl"])
            if k in hold.index:
                x = hold.loc[k]
                hh.append({"set": e["set"], "H": e["H"], "tp": e["tp"], "sl": e["sl"], "dev_ev": e["ev"],
                           "dev_win": e["win"], "hold_n": x["n"], "hold_ev": x["ev"], "hold_win": x["win"],
                           "hold_alpha": x["alpha"]})
        if hh:
            L.append(pd.DataFrame(hh).round(2).to_string(index=False))
    L.append("\n■ TỐT NHẤT MỖI BỘ LỌC (DEV, H ≤ 10, chốt ≥ +2.5%, xếp theo lãi TB)")
    g = dev[(dev["tp"] >= 2.5) & (dev["H"] <= 10)].sort_values("ev", ascending=False).groupby("set").head(1)
    L.append(fmt(g, cols))
    txt = "\n".join(L)
    print(txt)
    with open(os.path.join(OUT, "v3_result.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    if best is not None:
        with open(os.path.join(OUT, "v3_rule.json"), "w", encoding="utf-8") as fh:
            json.dump({k: (None if pd.isna(v) else (float(v) if isinstance(v, (int, float, np.floating)) else str(v)))
                       for k, v in best.items()}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    t0 = time.time()
    main()
    log.info("Xong sau %.0f giây", time.time() - t0)
