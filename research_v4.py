"""
NGHIÊN CỨU CLAUDE V4 – ÍT LỆNH, TỶ LỆ THẮNG CAO (bộ lọc khắt khe).

Mục tiêu người dùng: không cần nhiều mã, có khi cả tháng 1 lệnh, miễn THẮNG.

LUẬT ĐÃ KHÓA TRƯỚC KHI CHẠY (không chỉnh theo Holdout):
  - Development 01/01/2023 → 23/04/2026. Holdout 24/04/2026 → hết dữ liệu.
  - Mua Open phiên sau tín hiệu, phí 0.4%, T+2.5 (bán từ phiên thứ 3).
  - Mỗi phiên: Close < cắt lỗ -> bán Open phiên sau (xét TRƯỚC); High >= chốt lời -> bán tại chốt lời; hết H phiên -> bán Close.
  - "Thắng" = lệnh có lãi sau phí.
  - Chốt lời tối thiểu +4% (để thắng là thắng thật, không phải chốt +1% cho đẹp tỷ lệ).
  - Hai cách dùng: (a) mua mọi mã đạt bộ lọc; (b) mỗi ngày chỉ mua 1 mã tốt nhất (điểm chất lượng cao nhất).
  - Chọn luật trên Development: n >= 36 (≈ ≥ 1 lệnh/tháng), lãi TB >= +1%/lệnh, tỷ lệ thắng >= 55% ở TỪNG năm 2023, 2024, 2025
    -> lấy luật có TỶ LỆ THẮNG cao nhất (bằng nhau thì lãi TB cao hơn).
  - Holdout ĐẠT khi: tỷ lệ thắng >= 60%, lãi TB > 0 và hơn VNINDEX cùng kỳ.
"""
import json
import logging
import os
import time

import numpy as np
import pandas as pd

import analysis as an
import config
from backtest import Ratios, feature_frame, row_to_f
from research_v3 import DEV_END, FEE, START, dedupe, load_data, path, simulate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("v4")
OUT = os.path.join(config.DATA_DIR, "research")

TARGETS = [0.04, 0.05, 0.06, 0.08]
STOPS = [0.05, 0.07, 0.10, None]
HORIZONS = [10, 15, 20]
BIG = 20e9          # "mã lớn": GTGD TB20 >= 20 tỷ


def build(data):
    idx = data["index"].reset_index(drop=True)
    idx["date"] = pd.to_datetime(idx["date"])
    ic = idx["close"]
    idx_up = dict(zip(idx["date"], (ic > ic.rolling(50).mean()) & (ic.rolling(50).mean() > ic.rolling(50).mean().shift(10))))
    idx_ret60 = dict(zip(idx["date"], ic / ic.shift(60) - 1))
    ratios = Ratios()
    feats, raw = {}, {}
    for s, df in data["prices"].items():
        if len(df) < config.MIN_BARS + 20:
            continue
        df = df.reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        ff = feature_frame(df)
        if np.nanmax(ff["value20"].values) < config.MIN_VALUE20_VND:
            continue
        feats[s] = ff.set_index("date")
        raw[s] = df
    panel = pd.concat(feats, names=["symbol", "date"]).reset_index()
    panel = panel[panel["value20"] >= config.MIN_VALUE20_VND]
    by_date = dict(tuple(panel.groupby("date")))
    pos = {s: pd.Index(raw[s]["date"]) for s in raw}
    sig = []
    for k, d in enumerate([d for d in idx["date"] if d >= START]):
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
            ok = bool(fd) and all(fd.get(x) is not None for x in ("margin", "roe", "pb"))
            p, m20, m50, m200 = f["price"], f["ma20"], f["ma50"], f["ma200"]
            rows.append({
                "symbol": r["symbol"], "p": pos[r["symbol"]].get_loc(d),
                "margin": fd["margin"] if ok else np.nan, "roe": fd["roe"] if ok else np.nan, "pb": fd["pb"] if ok else np.nan,
                "atr_pct": f["atr"] / p, "adx": f["adx"], "rsi": f["rsi"], "value20": f["value20"],
                "trend": p > m50 and f["rsi"] >= 50,
                "full_trend": bool(m200) and p > m20 > m50 > m200,
                "pullback": p <= m20 + 0.5 * f["atr"] and p > m50,
                "rsi_ok": 50 <= f["rsi"] <= 65,
                "near_high": f["high252"] and p >= 0.9 * f["high252"],
                "rs60": (f["ret60"] - idx_ret60.get(d, np.nan)) if f["ret60"] is not None else np.nan,
                "big": f["value20"] >= BIG,
            })
        if len(rows) < 20:
            continue
        t = pd.DataFrame(rows)
        q = t.dropna(subset=["margin", "roe", "pb", "atr_pct", "adx"]).copy()
        q["score"] = (q["margin"].rank(pct=True) + q["roe"].rank(pct=True) + q["pb"].rank(pct=True)
                      + (1 - q["atr_pct"].rank(pct=True)) + q["adx"].rank(pct=True)) / 5
        q["rank"] = q["score"].rank(ascending=False)
        q["lowvol"] = q["atr_pct"].rank(pct=True) <= 0.3
        t = t.merge(q[["symbol", "score", "rank", "lowvol"]], on="symbol", how="left")
        t["date"] = d
        t["mkt_up"] = bool(idx_up.get(d, False))
        sig.append(t)
        if k % 100 == 0:
            log.info("Đã xử lý tới %s", d.date())
            ratios.save()
    ratios.save()
    s = pd.concat(sig, ignore_index=True)
    for c in ("lowvol", "near_high", "full_trend", "pullback", "big"):
        s[c] = s[c].fillna(False).astype(bool)
    s["rs_pos"] = s["rs60"] > 0
    return s, raw, idx


R = lambda t, n: t["rank"] <= n   # noqa: E731
SETS = {
    "S0 Tất cả mã thanh khoản (mốc)": lambda t: pd.Series(True, index=t.index),
    "A Top10 chất lượng + xu hướng đủ + RSI50-65 + VNI tốt": lambda t: R(t, 10) & t.full_trend & t.rsi_ok & t.mkt_up,
    "B Top10 + xu hướng đủ + nhịp chỉnh + VNI tốt": lambda t: R(t, 10) & t.full_trend & t.pullback & t.mkt_up,
    "C Top20 + xu hướng đủ + ADX≥25 + RSI50-65": lambda t: R(t, 20) & t.full_trend & (t.adx >= 25) & t.rsi_ok,
    "D Mã lớn + xu hướng đủ + RSI50-65 + VNI tốt": lambda t: t.big & t.full_trend & t.rsi_ok & t.mkt_up,
    "E Mã lớn + Top30 + nhịp chỉnh + VNI tốt": lambda t: t.big & R(t, 30) & t.pullback & t.mkt_up,
    "F Top10 + gần đỉnh 52 tuần + mạnh hơn VNI 60 phiên + RSI≤70": lambda t: R(t, 10) & t.near_high & t.rs_pos & (t.rsi <= 70),
    "G Top10 + xu hướng đủ + biến động thấp + VNI tốt": lambda t: R(t, 10) & t.full_trend & t.lowvol & t.mkt_up,
    "H Mã lớn + xu hướng đủ + mạnh hơn VNI + nhịp chỉnh": lambda t: t.big & t.full_trend & t.rs_pos & t.pullback,
    "I Mã lớn + Top30 + xu hướng đủ + RSI50-65 + nhịp chỉnh + VNI tốt": lambda t: t.big & R(t, 30) & t.full_trend & t.rsi_ok & t.pullback & t.mkt_up,
    "J Top10 + xu hướng đủ + RSI50-65 + ADX≥25 + VNI tốt": lambda t: R(t, 10) & t.full_trend & t.rsi_ok & (t.adx >= 25) & t.mkt_up,
    "K Mã lớn + Top20 + xu hướng đủ + mạnh hơn VNI + VNI tốt": lambda t: t.big & R(t, 20) & t.full_trend & t.rs_pos & t.mkt_up,
}


def run_grid(sig, raw, idx):
    ipos = pd.Index(idx["date"])
    io, icl = idx["open"].values, idx["close"].values
    rows = []
    for sname, fn in SETS.items():
        base = sig[fn(sig).fillna(False).astype(bool)]
        for mode in ("all", "top1"):
            if sname.startswith("S0") and mode == "top1":
                continue
            b = base
            if mode == "top1":
                b = base.sort_values("score", ascending=False).groupby("date").head(1)
            for H in HORIZONS:
                s = dedupe(b, H)
                if mode == "top1":   # chỉ giữ 1 lệnh tại 1 thời điểm: lệnh mới khi lệnh cũ đã đóng (H phiên)
                    s = s.sort_values("date")
                    keep, last = [], None
                    for i, d in zip(s.index, s["date"]):
                        di = ipos.get_loc(d)
                        if last is None or di - last >= H:
                            keep.append(i)
                            last = di
                    s = s.loc[keep]
                paths = []
                for r in s.itertuples():
                    pth = path(raw[r.symbol], r.p, H)
                    ip = ipos.get_loc(r.date)
                    if pth is None or ip + H + 1 >= len(icl):
                        continue
                    paths.append((r.date, pth, ip, r.symbol))
                if len(paths) < 10:
                    continue
                dates = pd.to_datetime([x[0] for x in paths])
                per = np.where(dates <= DEV_END, "DEV", "HOLDOUT")
                years = dates.year
                for tgt in TARGETS:
                    for stop in STOPS:
                        res = [simulate(x[1], tgt, stop, H) for x in paths]
                        ret = np.array([x[0] for x in res])
                        held = np.array([x[1] for x in res])
                        vni = np.array([(icl[x[2] + 1 + hd] / io[x[2] + 1] - 1 - FEE) * 100 for x, hd in zip(paths, held)])
                        for pn in ("DEV", "HOLDOUT"):
                            m = per == pn
                            if m.sum() < 5:
                                continue
                            a = ret[m] - vni[m]
                            row = {"set": sname, "mode": mode, "H": H, "tp": tgt * 100, "sl": stop * 100 if stop else None,
                                   "period": pn, "n": int(m.sum()), "win": (ret[m] > 0).mean() * 100, "ev": ret[m].mean(),
                                   "median": float(np.median(ret[m])), "worst": ret[m].min(), "vni": vni[m].mean(),
                                   "alpha": a.mean(), "hold": held[m].mean() + 1,
                                   "per_month": m.sum() / (40 if pn == "DEV" else 6)}
                            if pn == "DEV":
                                for y in (2023, 2024, 2025):
                                    my = m & (years == y)
                                    row[f"win{y}"] = (ret[my] > 0).mean() * 100 if my.sum() >= 3 else np.nan
                                    row[f"n{y}"] = int(my.sum())
                            rows.append(row)
                log.info("Xong %s | %s | H=%d | %d lệnh", sname, mode, H, len(paths))
    return pd.DataFrame(rows)


def report(r):
    os.makedirs(OUT, exist_ok=True)
    r.to_csv(os.path.join(OUT, "v4_grid.csv"), index=False, encoding="utf-8-sig")
    dev = r[r.period == "DEV"].copy()
    hold = r[r.period == "HOLDOUT"].set_index(["set", "mode", "H", "tp", "sl"])
    elig = dev[(dev.n >= 36) & (dev.ev >= 1.0) & (dev[["win2023", "win2024", "win2025"]] >= 55).all(axis=1)
               & (~dev.set.str.startswith("S0"))].sort_values(["win", "ev"], ascending=False)
    cols = ["set", "mode", "H", "tp", "sl", "n", "per_month", "win", "ev", "median", "worst", "vni", "alpha"]
    L = ["=" * 110, "NGHIÊN CỨU CLAUDE V4 – ÍT LỆNH, TỶ LỆ THẮNG CAO", "=" * 110,
         f"Số cấu hình đã thử: {len(dev)} | Development 2023 → {DEV_END.date()} | Holdout sau đó",
         "win = % lệnh lãi sau phí, ev = lãi TB/lệnh %, per_month = số lệnh/tháng, worst = lệnh lỗ nặng nhất, alpha = hơn VNINDEX", ""]
    s0 = dev[dev.set.str.startswith("S0")].sort_values("win", ascending=False).head(3)
    L += ["■ MỐC: MUA ĐẠI MỌI MÃ THANH KHOẢN (DEV)", s0[cols].round(2).to_string(index=False), ""]
    L += [f"■ CÁC LUẬT ĐẠT ĐIỀU KIỆN TRÊN DEVELOPMENT: {len(elig)}"]
    if len(elig):
        L.append(elig.head(15)[cols + ["win2023", "win2024", "win2025"]].round(1).to_string(index=False))
        best = elig.iloc[0]
        key = (best.set, best["mode"], best.H, best.tp, best.sl)
        L += ["", "■ LUẬT ĐƯỢC CHỌN (khóa từ Development):",
              f"  {best.set} | {'mỗi ngày 1 mã tốt nhất' if best['mode'] == 'top1' else 'mọi mã đạt lọc'} | giữ tối đa {int(best.H)} phiên | "
              f"chốt +{best.tp:.0f}% | cắt lỗ {('-' + format(best.sl, '.0f') + '%') if pd.notna(best.sl) else 'không'}",
              "  DEV    : " + ", ".join(f"{c}={best[c]:.2f}" for c in cols[5:])]
        s0 = r[r.set.str.startswith("S0") & (r.H == best.H) & (r.tp == best.tp)
               & ((r.sl == best.sl) | (r.sl.isna() & pd.isna(best.sl)))].set_index("period")
        for pn in ("DEV", "HOLDOUT"):
            if pn in s0.index:
                L.append(f"  Mốc mua đại cùng luật thoát {pn}: win={s0.loc[pn, 'win']:.1f}, ev={s0.loc[pn, 'ev']:.2f}")
        if key in hold.index:
            h = hold.loc[key]
            ok = h.win >= 60 and h.ev > 0 and h.alpha > 0
            L.append("  HOLDOUT: " + ", ".join(f"{c}={h[c]:.2f}" for c in cols[5:]))
            L.append(f"  ➜ HOLDOUT {'ĐẠT' if ok else 'KHÔNG ĐẠT'} (cần thắng ≥60%, lãi TB >0, hơn VNINDEX)")
        else:
            L.append("  HOLDOUT: không đủ lệnh để đánh giá")
        hh = []
        for _, e in elig.head(10).iterrows():
            k = (e.set, e["mode"], e.H, e.tp, e.sl)
            if k in hold.index:
                x = hold.loc[k]
                hh.append({"set": e.set[:40], "mode": e["mode"], "H": e.H, "tp": e.tp, "sl": e.sl, "dev_n": e.n, "dev_win": e.win,
                           "dev_ev": e.ev, "hold_n": x.n, "hold_win": x.win, "hold_ev": x.ev, "hold_alpha": x.alpha})
        if hh:
            L += ["", "■ 10 LUẬT ĐẦU TRÊN HOLDOUT (chỉ để xem, KHÔNG chọn lại):", pd.DataFrame(hh).round(1).to_string(index=False)]
    L += ["", "■ TỶ LỆ THẮNG CAO NHẤT MỖI BỘ LỌC (DEV, n ≥ 20, lãi TB > 0)"]
    g = dev[(dev.n >= 20) & (dev.ev > 0)].sort_values(["win", "ev"], ascending=False).groupby(["set", "mode"]).head(1)
    L.append(g[cols].round(1).to_string(index=False))
    txt = "\n".join(L)
    print(txt)
    with open(os.path.join(OUT, "v4_result.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt)


if __name__ == "__main__":
    t0 = time.time()
    data = load_data()
    sig, raw, idx = build(data)
    log.info("%d dòng mã-ngày", len(sig))
    report(run_grid(sig, raw, idx))
    log.info("Xong sau %.0f giây", time.time() - t0)
