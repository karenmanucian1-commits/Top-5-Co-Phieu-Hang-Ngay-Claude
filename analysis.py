"""
Phân tích từng mã: kỹ thuật, tài chính, vùng mua/bán/cắt lỗ, chấm điểm.
"""
import math

import numpy as np
import pandas as pd

import config
import indicators as ind


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# =============================================================================
# 1. ĐẶC TRƯNG KỸ THUẬT
# =============================================================================
def technical_features(df, partial):
    """
    df: giá ngày của 1 mã (phiên cuối có thể là phiên đang giao dịch dở = partial).
    Trả về (features dict, None) hoặc (None, lý do loại).
    """
    completed = df.iloc[:-1] if partial else df
    if len(completed) < config.MIN_BARS:
        return None, "ít dữ liệu"

    last20 = completed.tail(20)
    value20 = float((last20["close"] * 1000 * last20["volume"]).mean())
    median_value20 = float((last20["close"] * 1000 * last20["volume"]).median())
    trade_days = int((last20["volume"] > 0).sum())

    feat = df.copy()
    feat["volume"] = feat["volume"].astype(float)
    if partial:
        # Quy đổi khối lượng phiên sáng ra cả ngày để không làm méo chỉ báo volume
        feat.loc[feat.index[-1], "volume"] = feat["volume"].iloc[-1] / config.MORNING_VOLUME_FRACTION

    c = feat["close"]
    price = float(c.iloc[-1])
    ma20 = ind.sma(c, 20)
    ma50 = ind.sma(c, 50)
    ma200 = ind.sma(c, 200)
    rsi14 = ind.rsi(c, 14)
    _, _, hist = ind.macd(c)
    adx14, pdi, mdi = ind.adx(feat, 14)
    atr14 = ind.atr(feat, config.ATR_PERIOD)

    def ret(n):
        return price / float(c.iloc[-1 - n]) - 1 if len(c) > n and c.iloc[-1 - n] > 0 else None

    vol20 = float(completed["volume"].tail(20).mean())
    vol5 = float(feat["volume"].tail(5).mean())
    tail = feat.tail(21)
    chg = tail["close"].diff().iloc[1:]
    vols = tail["volume"].iloc[1:]
    up_vol = float(vols[chg > 0].sum())
    down_vol = float(vols[chg < 0].sum())

    lb = config.MA50_SLOPE_LOOKBACK
    f = {
        "price": price,
        "last_date": feat["date"].iloc[-1],
        "value20": value20,
        "median_value20": median_value20,
        "trade_days": trade_days,
        "ma20": float(ma20.iloc[-1]),
        "ma50": float(ma50.iloc[-1]),
        "ma200": float(ma200.iloc[-1]) if not np.isnan(ma200.iloc[-1]) else None,
        "ma50_prev": float(ma50.iloc[-1 - lb]),
        "rsi": float(rsi14.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]),
        "macd_hist_prev": float(hist.iloc[-4]),
        "adx": float(adx14.iloc[-1]),
        "pdi": float(pdi.iloc[-1]),
        "mdi": float(mdi.iloc[-1]),
        "atr": float(atr14.iloc[-1]),
        "ret5": ret(5), "ret20": ret(20), "ret60": ret(60),
        "high252": float(feat["high"].tail(252).max()),
        "vol_ratio": vol5 / vol20 if vol20 > 0 else None,
        "updown_vol": up_vol / down_vol if down_vol > 0 else 3.0,
    }
    return f, None


def liquidity_filter(f, today):
    if f["value20"] < config.MIN_VALUE20_VND:
        return "thanh khoản < 3 tỷ"
    if f["median_value20"] < config.MIN_MEDIAN_VALUE20_VND:
        return "thanh khoản không đều"
    if f["trade_days"] < config.MIN_TRADE_DAYS:
        return "ít phiên giao dịch"
    if f["price"] < config.MIN_PRICE_K:
        return "giá < 5.000đ"
    if (pd.Timestamp(today) - pd.Timestamp(f["last_date"])).days > config.MAX_STALE_DAYS:
        return "dữ liệu cũ"
    return None


def trend_filter(f):
    if config.REQUIRE_ABOVE_MA50 and f["price"] <= f["ma50"]:
        return "giá dưới MA50"
    if f["ma50_prev"] > 0 and f["ma50"] / f["ma50_prev"] - 1 < config.MA50_SLOPE_MIN:
        return "MA50 đang đi xuống"
    return None


def technical_score(f):
    """Điểm kỹ thuật 0-100. rs_pct20 / rs_pct60 (0-1) được gán sẵn từ main."""
    p = f["price"]
    s = 0.0
    detail = {}

    # A. Cấu trúc xu hướng (25)
    a = 0
    a += 4 if p > f["ma20"] else 0
    a += 6 if f["ma20"] > f["ma50"] else 0
    if f["ma200"]:
        a += 6 if f["ma50"] > f["ma200"] else 0
        a += 4 if p > f["ma200"] else 0
    a += 5 if p >= 0.85 * f["high252"] else 0
    detail["trend"] = a
    s += a

    # B. Sức mạnh tương đối so với VN-Index (25)
    b = 25 * (0.5 * f.get("rs_pct20", 0.5) + 0.5 * f.get("rs_pct60", 0.5))
    detail["rs"] = b
    s += b

    # C. Động lượng (20)
    r = f["rsi"]
    if 50 <= r <= 65:
        c_ = 10
    elif 65 < r <= 72:
        c_ = 6
    elif 45 <= r < 50:
        c_ = 5
    elif r > 72:
        c_ = 2
    else:
        c_ = 0
    c_ += 5 if f["macd_hist"] > 0 else 0
    c_ += 5 if f["macd_hist"] > f["macd_hist_prev"] else 0
    detail["momentum"] = c_
    s += c_

    # D. Dòng tiền (15)
    vr = f["vol_ratio"] or 0
    d1 = 7.5 if 1.0 <= vr <= 2.5 else (4 if 0.8 <= vr < 1.0 or 2.5 < vr <= 4 else 1)
    ud = f["updown_vol"]
    d2 = 7.5 if ud >= 1.3 else (5 if ud >= 1.0 else (2 if ud >= 0.8 else 0))
    detail["flow"] = d1 + d2
    s += d1 + d2

    # E. Độ mạnh xu hướng ADX (15)
    if f["pdi"] > f["mdi"]:
        e = 15 if 20 <= f["adx"] <= 40 else (10 if f["adx"] > 40 else 6)
    else:
        e = 0
    detail["adx"] = e
    s += e

    return round(s, 1), detail


# =============================================================================
# 2. TÀI CHÍNH DOANH NGHIỆP
# =============================================================================
def _first(row, keys):
    for k in keys:
        if k in row and _f(row[k]) is not None:
            return _f(row[k])
    return None


def parse_ratios(recs):
    """Chuẩn hoá bản ghi chỉ số tài chính thô từ VCI về các chỉ tiêu cần dùng."""
    if not recs:
        return None
    df = pd.DataFrame(recs)
    ycol = next((c for c in ["year", "yearReport", "ratioYear"] if c in df.columns), None)
    qcol = next((c for c in ["quarter", "lengthReport", "ratioQuarter"] if c in df.columns), None)
    if ycol:
        df[ycol] = pd.to_numeric(df[ycol], errors="coerce")
    if qcol:
        df[qcol] = pd.to_numeric(df[qcol], errors="coerce")
        q = df[df[qcol].between(1, 4)]
        if len(q):
            df = q
    sort_cols = [c for c in [ycol, qcol] if c]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=False)
    df = df.reset_index(drop=True)

    # Tự nhận biết đơn vị % (0.15) hay (15)
    def pct_scale(keys):
        vals = []
        for k in keys:
            if k in df.columns:
                vals += [abs(v) for v in pd.to_numeric(df[k], errors="coerce").dropna().tolist()]
        return 100.0 if vals and max(vals) <= 1.5 else 1.0

    roe_keys = ["roe"]
    margin_keys = ["afterTaxProfitMargin", "netProfitMargin"]
    roe_k, margin_k = pct_scale(roe_keys), pct_scale(margin_keys)

    row = df.iloc[0].to_dict()
    out = {
        "roe": (_first(row, roe_keys) or 0) * roe_k if _first(row, roe_keys) is not None else None,
        "pe": _first(row, ["pe"]),
        "pb": _first(row, ["pb"]),
        "de": _first(row, ["debtPerEquity", "debtToEquity"]),
        "margin": _first(row, margin_keys) * margin_k if _first(row, margin_keys) is not None else None,
        "eps": _first(row, ["earningPerShare", "eps", "epsTTM"]),
        "eps_growth": None,
        "roe_prev": None,
        "period": "",
    }
    if ycol and qcol and pd.notna(row.get(ycol)):
        out["period"] = f"Q{int(row[qcol])}/{int(row[ycol])}" if pd.notna(row.get(qcol)) else str(int(row[ycol]))
    def ttm_profit(r):
        # Lợi nhuận 4 quý gần nhất = Vốn hoá / P/E (dữ liệu VCI là chỉ số TTM)
        mc, pe = _first(r, ["marketCap"]), _first(r, ["pe"])
        if mc and pe and pe > 0:
            return mc / pe
        return None

    out["profit_ttm"] = ttm_profit(row)
    if len(df) > 4:
        old = df.iloc[4].to_dict()
        p_new, p_old = out["profit_ttm"], ttm_profit(old)
        eps_old = _first(old, ["earningPerShare", "eps", "epsTTM"])
        if p_new and p_old:
            out["eps_growth"] = (p_new - p_old) / abs(p_old) * 100
        elif out["eps"] is not None and eps_old not in (None, 0):
            out["eps_growth"] = (out["eps"] - eps_old) / abs(eps_old) * 100
        r_old = _first(old, roe_keys)
        out["roe_prev"] = r_old * roe_k if r_old is not None else None
    return out


def fundamental_filter(fd):
    if fd is None:
        return None  # thiếu dữ liệu: không loại, nhưng không được làm Ưu tiên #1
    if config.REQUIRE_POSITIVE_EPS:
        if (fd["eps"] is not None and fd["eps"] <= 0) or (fd["pe"] is not None and fd["pe"] <= 0):
            return "đang lỗ"
    if fd["roe"] is not None and fd["roe"] < config.MIN_ROE_PCT:
        return f"ROE thấp ({fd['roe']:.1f}%)"
    return None


def fundamental_score(fd, com_type):
    """Điểm tài chính 0-100. Chỉ tiêu nào thiếu thì bỏ khỏi mẫu số."""
    if fd is None:
        return 40.0, {}
    financial = com_type in ("NH", "CK", "BH")
    got, maxp = 0.0, 0.0
    d = {}

    roe = fd["roe"]
    if roe is not None:
        p = 25 if roe >= 20 else 20 if roe >= 15 else 13 if roe >= 10 else 6 if roe >= 5 else 0
        got += p; maxp += 25; d["roe"] = p

    g = fd["eps_growth"]
    if g is not None:
        p = 25 if g >= 30 else 20 if g >= 15 else 12 if g >= 0 else 5 if g >= -15 else 0
        got += p; maxp += 25; d["growth"] = p

    pe = fd["pe"]
    if pe is not None:
        p = 0 if pe <= 0 else 20 if pe <= 10 else 16 if pe <= 15 else 10 if pe <= 20 else 5 if pe <= 30 else 0
        got += p; maxp += 20; d["pe"] = p

    if financial:
        got += 9 + 9; maxp += 30  # ngân hàng/CK/BH: nợ & biên LN không so được như DN thường
    else:
        de = fd["de"]
        if de is not None:
            p = 15 if de <= 0.5 else 11 if de <= 1 else 6 if de <= 2 else 0
            got += p; maxp += 15; d["de"] = p
        m = fd["margin"]
        if m is not None:
            p = 15 if m >= 15 else 10 if m >= 8 else 5 if m >= 3 else 0
            got += p; maxp += 15; d["margin"] = p

    if maxp == 0:
        return 40.0, d
    return round(got / maxp * 100, 1), d


# =============================================================================
# 3. VÙNG MUA / BÁN / CẮT LỖ
# =============================================================================
STATUS_TEXT = {
    "MUA": "✅ MUA ĐƯỢC – giá đang trong vùng mua",
    "RR_KEM": "⚠️ Trong vùng mua nhưng R:R chưa đạt – chờ",
    "CHO": "⏳ CHỜ giá điều chỉnh về vùng mua",
    "DUOI": "🚫 Đã chạy xa vùng mua – KHÔNG mua đuổi",
    "NONG": "🚫 RSI quá nóng – KHÔNG mua đuổi",
    "DUOI_VUNG": "⚠️ Thủng hỗ trợ ngắn hạn – đứng ngoài",
    "GAY": "❌ Đã thủng mức cắt lỗ – loại",
}


def compute_zones(df, f):
    price, a = f["price"], f["atr"]
    look = df.tail(60)
    lows = ind.swing_points(look["low"], 3, "low") + [float(look["low"].tail(10).min())]
    cands = [v for v in lows + [f["ma20"], f["ma50"]] if v and v <= price * 1.003]
    support = max(cands) if cands else price - a

    z_low = support
    z_high = support + config.BUY_ZONE_ATR * a
    in_zone = z_low * 0.995 <= price <= z_high * 1.005
    entry = price if in_zone else (z_low + z_high) / 2

    stop = support - config.STOP_ATR * a
    capped = False
    if (entry - stop) / entry > config.MAX_RISK_PCT:
        stop = entry * (1 - config.MAX_RISK_PCT)
        capped = True
    elif (entry - stop) / entry < config.MIN_RISK_PCT:
        stop = entry * (1 - config.MIN_RISK_PCT)
    risk = entry - stop

    highs = ind.swing_points(df.tail(250)["high"], 5, "high") + [float(df.tail(250)["high"].max())]
    res = sorted({round(h, 2) for h in highs if h >= entry * 1.03})
    breakout = not res
    t1 = res[0] if res else entry + 2 * risk
    nxt = [h for h in res if h >= t1 * 1.03]
    t2 = nxt[0] if nxt else max(t1 + risk, entry + 3 * risk)
    # Giới hạn mục tiêu cho thực tế (kháng cự quá xa không dùng làm mục tiêu ngắn hạn)
    t1 = min(t1, entry * (1 + config.T1_MAX_PCT), entry + 2.5 * risk)
    t2 = min(max(t2, t1 * 1.03), entry * (1 + config.T2_MAX_PCT), entry + 4 * risk)
    t2 = max(t2, t1)

    fee = entry * config.FEE_ROUNDTRIP
    rr1 = (t1 - entry - fee) / (risk + fee) if risk > 0 else 0
    rr2 = (t2 - entry - fee) / (risk + fee) if risk > 0 else 0

    if price < stop:
        status = "GAY"
    elif f["rsi"] > config.RSI_OVERHEAT:
        status = "NONG"
    elif in_zone:
        status = "MUA" if rr1 >= config.MIN_RR else "RR_KEM"
    elif price < z_low:
        status = "DUOI_VUNG"
    elif (price - z_high) / z_high <= config.CHASE_LIMIT_PCT:
        status = "CHO"
    else:
        status = "DUOI"

    return {
        "support": support, "zone_low": z_low, "zone_high": z_high, "entry": entry,
        "stop": stop, "stop_capped": capped, "risk_pct": risk / entry * 100,
        "t1": t1, "t2": t2, "rr1": rr1, "rr2": rr2, "breakout": breakout,
        "status": status, "dist_zone_pct": (price - z_high) / z_high * 100,
    }


def setup_score(z, f):
    st = z["status"]
    if st == "MUA" or st == "RR_KEM":
        pos = 40
    elif st == "CHO":
        pos = 30 if z["dist_zone_pct"] <= 2 else 18
    elif st in ("DUOI", "NONG"):
        pos = 5
    else:
        pos = 0
    rr = z["rr1"]
    rrp = 35 if rr >= 2.5 else 28 if rr >= 2 else 20 if rr >= 1.5 else 8 if rr >= 1 else 0
    rk = z["risk_pct"]
    rkp = 15 if rk <= 5 else 10 if rk <= 7 else 4
    ext = (f["price"] - f["ma20"]) / f["atr"] if f["atr"] > 0 else 9
    exp_ = 10 if ext <= 1.5 else 5 if ext <= 3 else 0
    return float(pos + rrp + rkp + exp_)


# =============================================================================
# 4. BỐI CẢNH THỊ TRƯỜNG
# =============================================================================
def market_regime(idx_df, breadth_pct):
    c = idx_df["close"]
    price = float(c.iloc[-1])
    ma50 = float(ind.sma(c, 50).iloc[-1])
    ma200 = float(ind.sma(c, 200).iloc[-1]) if len(c) >= 200 else ma50
    ret20 = price / float(c.iloc[-21]) - 1 if len(c) > 21 else 0
    pts = 0
    pts += 1 if price > ma50 else 0
    pts += 1 if price > ma200 else 0
    pts += 1 if ma50 > ma200 else 0
    pts += 1 if breadth_pct >= 50 else 0
    if pts >= 3:
        regime, exposure = "🟢 TỐT", "có thể giải ngân 60-80%"
    elif pts == 2:
        regime, exposure = "🟡 TRUNG TÍNH", "giải ngân 30-50%, ưu tiên mã mạnh nhất"
    else:
        regime, exposure = "🔴 XẤU", "chỉ mua thăm dò ≤20%, giữ tiền là chính"
    return {
        "price": price, "ma50": ma50, "ma200": ma200, "ret20": ret20 * 100,
        "breadth": breadth_pct, "regime": regime, "exposure": exposure, "points": pts,
    }
