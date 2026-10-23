"""
CLAUDE BOT V2 – "Cổ phiếu chất lượng, giá ổn định".

Công thức được chọn từ backtest toàn thị trường 2023–2025 và kiểm tra trên 2026 (chưa từng nhìn):
  Điểm = trung bình thứ hạng (%) trong ngày của 5 yếu tố:
    1. Biên lợi nhuận sau thuế   (cao tốt)
    2. ROE                       (cao tốt)
    3. P/B                       (cao tốt – thị trường trả giá cao cho DN tốt)
    4. Biến động ATR%            (THẤP tốt)
    5. ADX – độ rõ xu hướng      (cao tốt)
  Rổ: các mã qua lọc thanh khoản giống V1 (không lọc xu hướng).
  Chọn 5 mã điểm cao nhất, tối đa 2 mã/ngành. Kế hoạch: mua quanh giá hiện tại, GIỮ ~20 PHIÊN.

Chạy:  python main_v2.py   |   python main_v2.py --dry
"""
import argparse
import html
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import analysis as an
import config
import report
import tracker
import vci_client as vci
from main import get_ratios_cached, load_cache, load_universe, save_cache

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
LOG_V2 = os.path.join(config.DATA_DIR, "picks_log_v2.csv")
LOG_V1 = os.path.join(config.DATA_DIR, "picks_log.csv")
HOLD_DAYS = 20
FACTORS = [("margin", True), ("roe", True), ("pb", True), ("atr_pct", False), ("adx", True)]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("v2")


def run(dry=False, client=vci, now=None):
    now = now or datetime.now(VN_TZ)
    today = now.date()

    symbols_df = client.get_all_symbols()
    industries = client.get_industries()
    _META.update(dict(zip(symbols_df["symbol"], symbols_df["exchange"])))
    idx_df = client.get_ohlc([config.INDEX_SYMBOL])[config.INDEX_SYMBOL]
    idx_last = pd.Timestamp(idx_df["date"].iloc[-1]).date()
    intraday = idx_last == today and now.weekday() < 5 and (now.hour, now.minute) < (15, 0)
    if intraday:
        session, label = "SANG", f"PHIÊN SÁNG {today:%d/%m/%Y}"
        note = "giá hiện tại phiên sáng; điểm tính trên dữ liệu đã đóng cửa"
    else:
        session = "CHIEU"
        label = f"TRƯỚC PHIÊN {today:%d/%m/%Y}" if idx_last < today else f"CUỐI NGÀY {idx_last:%d/%m/%Y}"
        note = f"tham chiếu giá đóng cửa {idx_last:%d/%m}"

    all_syms = symbols_df["symbol"].tolist()
    cached = load_universe(now)
    fetch = [s for s in cached if s in set(all_syms)] if cached else all_syms
    price_map = client.get_ohlc(fetch)
    log.info("Tải giá %d mã", len(price_map))

    # ---- Lọc thanh khoản (giống V1). Tính điểm trên phiên đã đóng cửa để không méo ATR/ADX ----
    rows = []
    for s, df in price_map.items():
        partial = intraday and pd.Timestamp(df["date"].iloc[-1]).date() == today
        base = df.iloc[:-1] if partial else df
        try:
            f, _ = an.technical_features(base, False)
        except Exception:  # noqa: BLE001
            continue
        if f is None or an.liquidity_filter(f, today):
            continue
        rows.append({"symbol": s, "f": f, "now_price": float(df["close"].iloc[-1]),
                     "prev_close": float(base["close"].iloc[-1]) if partial else float(df["close"].iloc[-2]),
                     "atr_pct": f["atr"] / f["price"], "adx": f["adx"]})
    log.info("Qua lọc thanh khoản: %d mã", len(rows))
    if not rows:
        raise RuntimeError("Không có mã nào qua lọc thanh khoản")

    # ---- Tài chính cho TOÀN BỘ mã qua lọc (có cache 5 ngày) ----
    cache = load_cache()
    for r in rows:
        try:
            fd = an.parse_ratios(get_ratios_cached(r["symbol"], cache, now, client))
        except Exception:  # noqa: BLE001
            fd = None
        r["fd"] = fd
        for k in ("margin", "roe", "pb", "pe", "de", "eps_growth"):
            r[k] = fd.get(k) if fd else None
    if not dry:
        save_cache(cache)

    df = pd.DataFrame(rows)
    need = [k for k, _ in FACTORS]
    ok = df.dropna(subset=need).copy()
    for k, high_good in FACTORS:
        rk = ok[k].astype(float).rank(pct=True)
        ok[f"r_{k}"] = rk if high_good else 1 - rk
    ok["score"] = ok[[f"r_{k}" for k, _ in FACTORS]].mean(axis=1) * 100
    ok = ok.sort_values("score", ascending=False)

    top, per = [], {}
    for _, r in ok.iterrows():
        sec = industries.get(r["symbol"], {}).get("sector", "Khác")
        if per.get(sec, 0) >= config.MAX_PER_SECTOR:
            continue
        r = r.to_dict()
        r["sector"] = sec
        top.append(r)
        per[sec] = per.get(sec, 0) + 1
        if len(top) >= config.TOP_N:
            break

    breadth = sum(1 for r in rows if r["f"]["price"] > r["f"]["ma50"]) / len(rows) * 100
    market = an.market_regime(idx_df, breadth)
    for x in top:
        x.update(advise(x, market))
    buy = [x for x in top if x["action"] == "MUA"]
    prio = max(buy, key=lambda x: x["score"]) if buy else None

    # ---- Hiệu quả thật: V2 và V1 (cùng thước đo) ----
    perf2 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V2) if session == "CHIEU" else []
    perf1 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V1) if session == "CHIEU" else []

    msgs = build(label, now, note, len(symbols_df), len(rows), len(ok), top, perf1, perf2, market, prio)
    if dry:
        for m in msgs:
            print(m)
            print("-" * 40)
        return top
    report.send_telegram(msgs)
    if top:
        tracker.log_picks([
            [idx_last.strftime("%Y-%m-%d"), session, i, x["symbol"], round(x["f"]["price"], 2),
             round(x["now_price"], 2), round(x["stop"], 2), round(x["t1"], 2), x["action"],
             int(prio is not None and x["symbol"] == prio["symbol"]), round(x["score"], 1)]
            for i, x in enumerate(top, 1)
        ], path=LOG_V2)
    return top


def advise(x, market):
    """
    Lời khuyên & vùng giá cho từng mã.
    Điều kiện MUA (kiểm tra trên dữ liệu: alpha 20 phiên cao hơn ~2% ở cả 2023-2025 và 2026):
        giá > MA50  và  RSI14 >= 50   (DN tốt + giá đang khoẻ)
    Ngược lại -> CHỜ (DN tốt nhưng giá đang yếu, mua sớm thường bị kẹp).
    Chốt lời/cắt lỗ dựa trên phân bố lợi nhuận 20 phiên của nhóm MUA trong backtest:
        25% tốt nhất lãi >= +4.7%, 10% tốt nhất >= +12%; 10% tệ nhất lỗ <= -7.7%.
    """
    f = x["f"]
    p, atr, ma20, ma50, rsi = x["now_price"], f["atr"], f["ma20"], f["ma50"], f["rsi"]
    above50 = p > ma50
    strong = rsi >= 50
    action = "MUA" if above50 and strong else "CHO"

    z_low = max(p - atr, ma50 if above50 else p - atr)
    z_high = p * 1.01
    stop_pct = min(max(2.5 * atr / p, 0.05), 0.08)          # 5%–8%
    stop = min(p * (1 - stop_pct), ma50 * 0.98) if above50 else p * (1 - stop_pct)
    stop = max(stop, p * 0.92)
    t1, t2 = p * 1.05, p * 1.12

    pros, cons = [], []
    if x["margin"] is not None and x["margin"] >= 20:
        pros.append(f"biên lợi nhuận rất cao {x['margin']:.0f}%")
    if x["roe"] is not None and x["roe"] >= 20:
        pros.append(f"ROE cao {x['roe']:.0f}%")
    if x["atr_pct"] <= 0.015:
        pros.append("giá rất ổn định, ít giật")
    if x["adx"] >= 30 and f["pdi"] > f["mdi"]:
        pros.append("xu hướng tăng rõ")
    if x.get("pe") and 0 < x["pe"] <= 10:
        pros.append(f"định giá rẻ (P/E {x['pe']:.1f})")
    if not above50:
        cons.append(f"giá dưới MA50 ({ma50:,.2f}) – xu hướng trung hạn đang yếu")
    if not strong:
        cons.append(f"RSI {rsi:.0f} < 50 – lực mua chưa quay lại")
    if f["pdi"] < f["mdi"] and x["adx"] >= 25:
        cons.append("ADX cao nhưng là xu hướng GIẢM")
    if x.get("pe") and x["pe"] > 25:
        cons.append(f"P/E cao ({x['pe']:.1f})")
    if rsi > 78:
        cons.append(f"RSI {rsi:.0f} rất cao – chia 2 lần mua")

    if action == "MUA":
        advice = "✅ <b>NÊN MUA</b> – doanh nghiệp tốt, giá đang khoẻ"
        if market["points"] <= 1:
            advice += " (thị trường xấu: chỉ mua ≤ 1/3 tỷ trọng dự định)"
    else:
        need = []
        if not above50:
            need.append(f"đóng cửa trên {ma50:,.2f} (MA50)")
        if not strong:
            need.append("RSI vượt 50")
        advice = "⏳ <b>CHỜ</b> – doanh nghiệp tốt nhưng giá đang yếu. Chỉ mua khi " + " và ".join(need)
    return {"action": action, "advice": advice, "zone_low": z_low, "zone_high": z_high,
            "stop": stop, "t1": t1, "t2": t2, "pros": pros[:3], "cons": cons[:3]}


def _pct(v):
    return "N/A" if v is None or pd.isna(v) else f"{v:+.1f}%"


def _perf_lines(name, perf):
    out = []
    for p in perf:
        out.append(f"• {name} ngày {p['date']} (sau {p['n']} phiên): TB {_pct(p['avg'])}, "
                   f"thắng {p['win']}/{p['total']} | VN-Index {_pct(p['idx'])}")
    return out


def build(label, now, note, n_all, n_liq, n_ok, top, perf1, perf2, market, prio):
    m = market
    L = [f"💎 <b>CLAUDE BOT V2 – CỔ PHIẾU CHẤT LƯỢNG</b> – {label}",
         f"🕐 {now:%H:%M %d/%m/%Y} | {note}",
         "",
         f"📊 <b>THỊ TRƯỜNG: {m['regime']}</b> – VN-Index {m['price']:,.2f} ({_pct(m['ret20'])} 20 phiên), "
         f"{m['breadth']:.0f}% mã trên MA50",
         f"• Tỷ trọng gợi ý: {m['exposure']}",
         "",
         f"🔎 Quét {n_all:,} mã → {n_liq} mã GTGD ≥3 tỷ → {n_ok} mã đủ dữ liệu tài chính",
         "📐 Chọn theo: Biên LN + ROE + P/B + Giá ít biến động + Xu hướng rõ"]
    if top:
        L += ["", "🏆 <b>TOP 5:</b> " + " · ".join(f"{x['symbol']}{'✅' if x['action'] == 'MUA' else '⏳'}" for x in top)]
        if prio:
            L.append(f"⭐ <b>ƯU TIÊN #1: {prio['symbol']}</b> – mua {prio['zone_low']:,.2f}–{prio['zone_high']:,.2f}, "
                     f"chốt lời {prio['t1']:,.2f} / {prio['t2']:,.2f}, cắt lỗ {prio['stop']:,.2f}")
        else:
            L.append("⭐ <b>ƯU TIÊN #1: CHƯA CÓ</b> – cả 5 mã đều đang yếu về giá, kiên nhẫn chờ")
    if perf1 or perf2:
        L += ["", "📒 <b>So sánh hiệu quả thật V1 vs V2</b>"] + _perf_lines("V2", perf2) + _perf_lines("V1", perf1)
    header = "\n".join(L)

    blocks = []
    for i, x in enumerate(top, 1):
        f = x["f"]
        p = x["now_price"]
        chg = (p / x["prev_close"] - 1) * 100 if x["prev_close"] else None
        star = " ⭐" if prio and x["symbol"] == prio["symbol"] else ""
        b = ["━━━━━━━━━━━━━━",
             f"<b>#{i} {x['symbol']}</b>{star} · {meta_ex(x)} · {html.escape(x['sector'])} · Điểm {x['score']:.0f}/100",
             f"💰 Giá: <b>{p:,.2f}</b> ({_pct(chg)})",
             x["advice"],
             f"🟢 Vùng mua: <b>{x['zone_low']:,.2f} – {x['zone_high']:,.2f}</b>",
             f"🎯 Chốt lời: <b>{x['t1']:,.2f}</b> (+5%, bán 1/2) → <b>{x['t2']:,.2f}</b> (+12%) – hoặc hết 20 phiên",
             f"🛑 Cắt lỗ: <b>{x['stop']:,.2f}</b> (−{(1 - x['stop'] / p) * 100:.1f}%) – đóng cửa dưới mức này thì bán",
             f"🏢 TC: Biên LN {x['margin']:.1f}% · ROE {x['roe']:.1f}% · P/B {x['pb']:.1f}"
             + (f" · P/E {x['pe']:.1f}" if x.get("pe") else ""),
             f"📈 KT: RSI {f['rsi']:.0f} · ADX {x['adx']:.0f} · MA50 {f['ma50']:,.2f} · "
             f"biến động {x['atr_pct'] * 100:.1f}%/phiên · GTGD {f['value20'] / 1e9:,.1f} tỷ"]
        if x["pros"]:
            b.append("👍 " + "; ".join(x["pros"]))
        if x["cons"]:
            b.append("👎 " + "; ".join(x["cons"]))
        blocks.append("\n".join(b))
    blocks.append("━━━━━━━━━━━━━━\n"
                  "📌 <b>Cách dùng V2:</b> chỉ mua mã ✅, chia 2 lần trong vùng mua. Tới +5% bán 1/2 và dời cắt lỗ về giá vốn; "
                  "phần còn lại giữ tới +12% hoặc hết ~20 phiên. Mã ⏳ chỉ theo dõi.\n"
                  "📊 Backtest: TOP5 V2 ngang VN-Index; lọc thêm 'giá > MA50 & RSI ≥ 50' giúp hơn ~2%/20 phiên ở cả 2023-25 và 2026. "
                  "Mức chốt lời/cắt lỗ lấy từ phân bố lãi lỗ lịch sử, chưa mô phỏng theo từng phiên.\n"
                  "<i>Bot tham khảo, không phải khuyến nghị đầu tư.</i>")
    return report.split_messages(header, blocks)


_META = {}


def meta_ex(x):
    return _META.get(x["symbol"], "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    try:
        run(dry=a.dry)
    except Exception as e:  # noqa: BLE001
        log.exception("Bot V2 lỗi")
        if not a.dry:
            try:
                report.send_telegram([f"⚠️ CLAUDE BOT V2 lỗi: {html.escape(str(e)[:500])}"])
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
