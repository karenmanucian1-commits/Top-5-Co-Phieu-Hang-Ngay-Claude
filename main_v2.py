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
import json
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
SCORE_RANK_LIMIT = 30   # chỉ xét mã nằm trong top 30 điểm V2
MAX_BUY = 10            # tối đa 10 mã nên mua mỗi lần
MAX_SECTOR_V2 = 3
HOLD_DAYS = 20
PROBS_PATH = os.path.join(config.DATA_DIR, "target_probs.json")


def load_probs():
    try:
        with open(PROBS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


PROBS = load_probs()


DEFAULT_PLAN = {"luot": ([2, 3, 5], 0.05, 10), "giu": ([5, 10, 15], 0.15, 60)}


def target_plan(atr_pct, key):
    """3 mốc chốt lời cho kiểu 'luot' (lướt sóng) hoặc 'giu' (nắm giữ) theo xác suất lịch sử.
    Mốc 1 ~ khả năng chạm >=60%, Mốc 2 ~ >=45%, Mốc 3 ~ >=30%."""
    lv_def, stop_def, hz_def = DEFAULT_PLAN[key]
    if not PROBS or key not in PROBS:
        return {"levels": [(m, None, None) for m in lv_def], "stop": stop_def, "horizon": hz_def, "stop_p": None}
    sec = PROBS[key]
    meta = PROBS["_meta"]
    grp = "all"
    for lo, hi, name in zip(meta["vol_bins"][:-1], meta["vol_bins"][1:], meta["vol_names"]):
        if lo <= atr_pct < hi and name in sec:
            grp = name
    tab = sec[grp]
    lv = sorted(int(k) for k in tab if k.isdigit())

    def pick(tp, after):
        c = [x for x in lv if x > after and tab[str(x)]["p"] >= tp]
        return max(c) if c else None

    m1 = pick(60, 0) or lv[0]
    m2 = pick(45, m1) or next((x for x in lv if x > m1), m1)
    m3 = pick(30, m2) or next((x for x in lv if x > m2), m2)
    levels = []
    for m in dict.fromkeys([m1, m2, m3]):
        levels.append((m, tab[str(m)]["p"], tab[str(m)]["days"]))
    return {"levels": levels, "stop": sec["stop"], "horizon": sec["horizon"], "stop_p": tab.get("stop")}


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

    breadth = sum(1 for r in rows if r["f"]["price"] > r["f"]["ma50"]) / len(rows) * 100
    market = an.market_regime(idx_df, breadth)

    # Chỉ lấy mã ✅ NÊN MUA trong nhóm top 30 điểm (backtest: mã ✅ trong top ~30 đều hiệu quả,
    # ngoài top 50 kém hẳn). Tối đa MAX_BUY mã, tối đa MAX_SECTOR_V2 mã/ngành.
    top, per = [], {}
    for rank, (_, r) in enumerate(ok.iterrows(), 1):
        if rank > SCORE_RANK_LIMIT or len(top) >= MAX_BUY:
            break
        r = r.to_dict()
        r.update(advise(r, market))
        if r["action"] != "MUA":
            continue
        sec = industries.get(r["symbol"], {}).get("sector", "Khác")
        if per.get(sec, 0) >= MAX_SECTOR_V2:
            continue
        r["sector"], r["rank"] = sec, rank
        top.append(r)
        per[sec] = per.get(sec, 0) + 1
    buy = top
    prio = max(buy, key=lambda x: x["score"]) if buy else None

    # ---- Hiệu quả thật: V2 và V1 (cùng thước đo) ----
    perf2 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V2) if session == "CHIEU" else []
    perf1 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V1) if session == "CHIEU" else []

    msgs = build(label, now, note, len(symbols_df), len(rows), len(ok), top, perf1, perf2, market, prio,
                 list(getattr(client, "MISSING", [])),
                 getattr(client, "SOURCE_NAME", {}).get(getattr(client, "SOURCE", None), ""))
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
    Mốc chốt lời: lấy từ data/target_probs.json (backtest_targets.py) – tỷ lệ giá chạm mốc
        trong 60 phiên trước khi dính cắt lỗ -15%, theo nhóm biến động của mã.
    """
    f = x["f"]
    p, atr, ma20, ma50, rsi = x["now_price"], f["atr"], f["ma20"], f["ma50"], f["rsi"]
    above50 = p > ma50
    strong = rsi >= 50
    action = "MUA" if above50 and strong else "CHO"

    z_low = max(p - atr, ma50 if above50 else p - atr)
    z_high = p * 1.01
    luot = target_plan(x["atr_pct"], "luot")
    giu = target_plan(x["atr_pct"], "giu")
    stop = p * (1 - giu["stop"])
    t1, t2 = p * (1 + giu["levels"][0][0] / 100), p * (1 + giu["levels"][-1][0] / 100)

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
        cons.append(f"RSI {rsi:.0f} dưới 50 – lực mua chưa quay lại")
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
        advice = "⏳ <b>CHỜ</b> – doanh nghiệp tốt nhưng giá đang yếu, chưa nên mua"
    return {"action": action, "advice": advice, "zone_low": z_low, "zone_high": z_high,
            "stop": stop, "t1": t1, "t2": t2, "luot": luot, "giu": giu, "pros": pros[:3], "cons": cons[:3]}


def _pct(v):
    return "N/A" if v is None or pd.isna(v) else f"{v:+.1f}%"


def _perf_lines(name, perf):
    out = []
    for p in perf:
        out.append(f"• {name} ngày {p['date']} (sau {p['n']} phiên): TB {_pct(p['avg'])}, "
                   f"thắng {p['win']}/{p['total']} | VN-Index {_pct(p['idx'])}")
    return out


def build(label, now, note, n_all, n_liq, n_ok, top, perf1, perf2, market, prio, missing=(), source=""):
    m = market
    L = [f"💎 <b>CLAUDE BOT V2</b> – {label}",
         f"🕐 {now:%H:%M %d/%m/%Y} | {note}" + (f" | nguồn giá: {source}" if source else ""),
         "",
         f"📊 <b>THỊ TRƯỜNG: {m['regime']}</b> – VN-Index {m['price']:,.2f} ({_pct(m['ret20'])} 20 phiên), "
         f"{m['breadth']:.0f}% mã trên MA50",
         f"• Tỷ trọng gợi ý: {m['exposure']}",
         "",
         f"🔎 Quét {n_all:,} mã → {n_liq} mã GTGD ≥3 tỷ → {n_ok} mã đủ dữ liệu tài chính",
         "📐 Chọn theo: Biên LN + ROE + P/B + Giá ít biến động + Xu hướng rõ"]
    if missing:
        L.append(f"⚠️ Thiếu dữ liệu {len(missing)} mã (nguồn dữ liệu lỗi): {', '.join(list(missing)[:15])}")
    if top:
        L += ["", f"✅ <b>CỔ PHIẾU NÊN MUA ({len(top)} mã):</b> " + " · ".join(x['symbol'] for x in top)]
        if prio:
            L.append(f"⭐ <b>ƯU TIÊN #1: {prio['symbol']}</b> – mua {prio['zone_low']:,.2f}–{prio['zone_high']:,.2f}, "
                     f"lướt sóng chốt " + " / ".join(f"{prio['now_price'] * (1 + m / 100):,.2f}" for m, *_ in prio['luot']['levels'])
                     + f"; nắm giữ chốt " + " / ".join(f"{prio['now_price'] * (1 + m / 100):,.2f}" for m, *_ in prio['giu']['levels']))
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
        ]
        if x["action"] != "MUA":
            ma50 = f["ma50"]
            b.append(f"🔔 Điểm mua: khi giá đóng cửa trên <b>{max(ma50, p):,.2f}</b>"
                     + ("" if f["rsi"] >= 50 else " và RSI vượt 50") + " – lúc đó bot sẽ chuyển ✅ và báo mốc chốt lời/cắt lỗ")
        for key, title in (("luot", "⚡ <b>LƯỚT SÓNG</b>"), ("giu", "📦 <b>NẮM GIỮ</b>")):
            if x["action"] != "MUA":
                break
            if key == "luot":
                b.append(f"🟢 Vùng mua: <b>{x['zone_low']:,.2f} – {x['zone_high']:,.2f}</b>")
            pl = x[key]
            b.append(f"{title} (tối đa {pl['horizon']} phiên, cắt lỗ −{pl['stop'] * 100:.0f}% = {p * (1 - pl['stop']):,.2f}):")
            n = len(pl["levels"])
            for k, (m, prob, days) in enumerate(pl["levels"], 1):
                frac = "hết" if k == n else f"1/{n}"
                ptxt = (f"{prob:.0f}% chạm" + (f", ~{days} phiên" if days else "")) if prob is not None else "chưa có thống kê"
                b.append(f"   Mốc {k}: <b>{p * (1 + m / 100):,.2f}</b> (+{m}%) → bán {frac} | {ptxt}")
            if pl.get("stop_p") is not None:
                b.append(f"   (khả năng dính cắt lỗ: {pl['stop_p']:.0f}%)")
        b += [
             f"🏢 TC: Biên LN {x['margin']:.1f}% · ROE {x['roe']:.1f}% · P/B {x['pb']:.1f}"
             + (f" · P/E {x['pe']:.1f}" if x.get("pe") else ""),
             f"📈 KT: RSI {f['rsi']:.0f} · ADX {x['adx']:.0f} · MA50 {f['ma50']:,.2f} · "
             f"biến động {x['atr_pct'] * 100:.1f}%/phiên · GTGD {f['value20'] / 1e9:,.1f} tỷ"]
        if x["pros"]:
            b.append("👍 " + "; ".join(x["pros"]))
        if x["cons"]:
            b.append("👎 " + "; ".join(x["cons"]))
        blocks.append("\n".join(b))
    if not top:
        blocks.append("━━━━━━━━━━━━━━\n⚠️ Hôm nay không có cổ phiếu nào đủ điều kiện mua – đứng ngoài, giữ tiền.")
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
