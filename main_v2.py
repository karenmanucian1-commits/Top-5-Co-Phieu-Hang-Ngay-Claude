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

    # ---- Hiệu quả thật: V2 và V1 (cùng thước đo) ----
    perf2 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V2) if session == "CHIEU" else []
    perf1 = tracker.evaluate(price_map, idx_df, (5, 20), path=LOG_V1) if session == "CHIEU" else []

    msgs = build(label, now, note, len(symbols_df), len(rows), len(ok), top, perf1, perf2)
    if dry:
        for m in msgs:
            print(m)
            print("-" * 40)
        return top
    report.send_telegram(msgs)
    if top:
        tracker.log_picks([
            [idx_last.strftime("%Y-%m-%d"), session, i, x["symbol"], round(x["f"]["price"], 2),
             round(x["now_price"], 2), round(stop_of(x), 2), None, "GIU20", int(i == 1), round(x["score"], 1)]
            for i, x in enumerate(top, 1)
        ], path=LOG_V2)
    return top


def stop_of(x):
    """Cắt lỗ thảm hoạ (chưa backtest): 3 ATR dưới giá, không xa quá 12%."""
    p, atr = x["now_price"], x["f"]["atr"]
    return max(p - 3 * atr, p * 0.88)


def _pct(v):
    return "N/A" if v is None or pd.isna(v) else f"{v:+.1f}%"


def _perf_lines(name, perf):
    out = []
    for p in perf:
        out.append(f"• {name} ngày {p['date']} (sau {p['n']} phiên): TB {_pct(p['avg'])}, "
                   f"thắng {p['win']}/{p['total']} | VN-Index {_pct(p['idx'])}")
    return out


def build(label, now, note, n_all, n_liq, n_ok, top, perf1, perf2):
    L = [f"💎 <b>CLAUDE BOT V2 – CỔ PHIẾU CHẤT LƯỢNG</b> – {label}",
         f"🕐 {now:%H:%M %d/%m/%Y} | {note}",
         "",
         f"🔎 Quét {n_all:,} mã → {n_liq} mã GTGD ≥3 tỷ → {n_ok} mã đủ dữ liệu tài chính",
         "📐 Điểm = Biên LN + ROE + P/B + Ít biến động + Xu hướng rõ (xếp hạng toàn rổ)",
         "🗓 Cách dùng: mua quanh giá hiện tại, <b>giữ khoảng 20 phiên</b> rồi xem lại. Không lướt sóng."]
    if top:
        L += ["", "🏆 <b>TOP 5:</b> " + " · ".join(x["symbol"] for x in top)]
    if perf1 or perf2:
        L += ["", "📒 <b>So sánh hiệu quả thật V1 vs V2</b>"] + _perf_lines("V2", perf2) + _perf_lines("V1", perf1)
    header = "\n".join(L)

    blocks = []
    for i, x in enumerate(top, 1):
        f = x["f"]
        price = x["now_price"]
        chg = (price / x["prev_close"] - 1) * 100 if x["prev_close"] else None
        b = ["━━━━━━━━━━━━━━",
             f"<b>#{i} {x['symbol']}</b> · {meta_ex(x)} · {html.escape(x['sector'])}",
             f"Điểm V2 <b>{x['score']:.0f}</b>/100",
             f"💰 Giá: <b>{price:,.2f}</b> ({_pct(chg)})",
             f"🟢 Vùng mua: <b>{price * 0.98:,.2f} – {price * 1.01:,.2f}</b> (quanh giá hiện tại, không đuổi quá +1%)",
             f"🎯 Kế hoạch bán: sau ~{HOLD_DAYS} phiên đánh giá lại; giữ tiếp nếu vẫn nằm trong TOP5",
             f"🛑 Cắt lỗ khẩn cấp: <b>{stop_of(x):,.2f}</b> (−{(1 - stop_of(x) / price) * 100:.1f}%, chưa backtest – chỉ để phòng sập mạnh)",
             f"🏢 Biên LN {x['margin']:.1f}% · ROE {x['roe']:.1f}% · P/B {x['pb']:.1f}"
             + (f" · P/E {x['pe']:.1f}" if x.get("pe") else ""),
             f"📈 Biến động {x['atr_pct'] * 100:.1f}%/phiên · ADX {x['adx']:.0f} · RSI {f['rsi']:.0f} · GTGD {f['value20'] / 1e9:,.1f} tỷ/phiên"]
        blocks.append("\n".join(b))
    blocks.append("━━━━━━━━━━━━━━\n"
                  "📊 Backtest 2026 (chưa từng nhìn khi thiết kế): V2 ngang VN-Index, hơn TB mã thanh khoản ~1.4%/20 phiên. "
                  "V1 kém VN-Index ~1%.\n<i>Bot tham khảo, không phải khuyến nghị đầu tư.</i>")
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
