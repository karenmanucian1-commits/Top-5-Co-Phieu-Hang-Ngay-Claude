"""
KIỂM TRA TOÀN DIỆN BOT V1 + V2 (chạy trên GitHub Actions, không gửi Telegram).

  python audit.py            -> ghi kết quả ra data/logs/audit.txt

Kiểm tra:
  1. Dữ liệu: quét lại TOÀN BỘ mã, so với danh sách thanh khoản đang lưu (universe.json) -> có sót mã không
  2. Giá: ngày giao dịch mới nhất của từng mã; so sánh giá Vietcap vs KBS trên mẫu mã
  3. Tài chính: bao nhiêu mã thanh khoản thiếu BCTC, kỳ BCTC mới nhất
  4. V1: mỗi mã đưa ra có đúng điều kiện không; mã MUA ĐƯỢC bị sót do giới hạn 60 mã BCTC / 10 mã / ngành
  5. V2: mỗi mã đưa ra có đúng điều kiện không; mã ✅ bị sót do top 30 / 10 mã / ngành
"""
import json
import os
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import analysis as an
import config
import main as m1
import main_v2 as m2
import vci_client as vci

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
OUT = os.path.join(config.DATA_DIR, "logs", "audit.txt")
L = []


def p(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    L.append(line)


def sec(t):
    p("")
    p("=" * 70)
    p(t)
    p("=" * 70)


def main():
    t0 = time.time()
    now = datetime.now(VN_TZ)
    p(f"KIỂM TRA TOÀN DIỆN BOT V1 + V2 – {now:%H:%M %d/%m/%Y}")

    # ------------------------------------------------------------------ 1
    sec("1. DỮ LIỆU GIÁ – QUÉT TOÀN THỊ TRƯỜNG")
    syms = vci.get_all_symbols()
    all_syms = syms["symbol"].tolist()
    p(f"Tổng mã niêm yết (HSX/HNX/UPCOM, 3 ký tự): {len(all_syms)}")
    idx = vci.get_ohlc([config.INDEX_SYMBOL])[config.INDEX_SYMBOL]
    last_day = pd.Timestamp(idx["date"].iloc[-1]).date()
    p(f"VN-Index phiên gần nhất: {last_day:%d/%m/%Y} – đóng cửa {idx['close'].iloc[-1]:,.2f}")
    prices = vci.get_ohlc(all_syms, max_minutes=40)
    p(f"Nguồn giá: {vci.SOURCE_NAME.get(vci.SOURCE)} | tải được {len(prices)}/{len(all_syms)} mã | "
      f"lỗi {len(vci.MISSING)}: {', '.join(vci.MISSING[:30])}")

    liquid_all, stale = [], []
    for s, df in prices.items():
        try:
            f, _ = an.technical_features(df, False)
        except Exception:  # noqa: BLE001
            continue
        if f is None:
            continue
        if not an.liquidity_filter(f, now.date()):
            liquid_all.append(s)
            if pd.Timestamp(f["last_date"]).date() < last_day:
                stale.append(f"{s}({pd.Timestamp(f['last_date']):%d/%m})")
    try:
        with open(os.path.join(config.DATA_DIR, "universe.json"), encoding="utf-8") as fh:
            uni = json.load(fh)
        uni_syms, uni_date = set(uni["symbols"]), uni["date"]
    except Exception:  # noqa: BLE001
        uni_syms, uni_date = set(), "?"
    miss_uni = sorted(set(liquid_all) - uni_syms)
    p(f"Mã qua lọc thanh khoản (≥3 tỷ...) khi quét toàn bộ: {len(liquid_all)}")
    p(f"Danh sách thanh khoản đang lưu (ngày {uni_date}): {len(uni_syms)} mã")
    p(f"➜ Mã đủ thanh khoản nhưng KHÔNG có trong danh sách lưu (bot hằng ngày sẽ bỏ sót): {len(miss_uni)}"
      + (f" – {', '.join(miss_uni)}" if miss_uni else " ✅"))
    p(f"➜ Mã thanh khoản có giá chưa cập nhật phiên {last_day:%d/%m}: {len(stale)}"
      + (f" – {', '.join(stale[:30])}" if stale else " ✅"))

    # ------------------------------------------------------------------ 2
    sec("2. ĐỐI CHIẾU GIÁ VIETCAP vs KBS (mẫu 25 mã thanh khoản lớn)")
    sample = sorted(liquid_all, key=lambda s: -float((prices[s]["close"] * prices[s]["volume"]).tail(20).mean()))[:25]
    diffs, bad = [], []
    for s in sample:
        try:
            a = vci._fetch_ohlc_batch([s], 30).get(s)
        except Exception:  # noqa: BLE001
            a = None
        try:
            b = vci._fetch_kbs(s, 30).get(s)
        except Exception:  # noqa: BLE001
            b = None
        if a is None or b is None:
            bad.append(f"{s}({'VCI' if a is None else ''}{'KBS' if b is None else ''} lỗi)")
            continue
        mm = a.merge(b, on="date", suffixes=("_v", "_k")).tail(20)
        if mm.empty:
            bad.append(f"{s}(không trùng ngày)")
            continue
        d = ((mm["close_v"] - mm["close_k"]).abs() / mm["close_v"] * 100).max()
        diffs.append((s, d, a["date"].iloc[-1].date(), b["date"].iloc[-1].date()))
        time.sleep(0.2)
    if diffs:
        worst = sorted(diffs, key=lambda x: -x[1])[:5]
        p(f"So được {len(diffs)} mã. Chênh lệch giá đóng cửa lớn nhất (20 phiên): "
          + ", ".join(f"{s} {d:.2f}%" for s, d, *_ in worst))
        same_day = sum(1 for _, _, dv, dk in diffs if dv == dk)
        p(f"Cùng ngày giao dịch mới nhất: {same_day}/{len(diffs)}")
        p("➜ " + ("✅ Hai nguồn khớp nhau (chênh < 0.5%)" if worst[0][1] < 0.5 else
                  "⚠️ Có chênh lệch – có thể do điều chỉnh cổ tức/chia tách khác nhau"))
    if bad:
        p(f"Không so được: {', '.join(bad)}")

    # ------------------------------------------------------------------ 3 + 4: V1
    sec("3. BOT V1 – KIỂM TRA ĐIỀU KIỆN & MÃ BỊ SÓT")
    ctx = m1.run(dry=True, now=now)
    top1 = ctx["top"]
    p(f"Phễu V1: {ctx['funnel']}")
    p(f"V1 đưa ra {len(top1)} mã: {', '.join(x['symbol'] for x in top1)}")
    errs = []
    for x in top1:
        z, f = x["z"], x["f"]
        if z["status"] != "MUA":
            errs.append(f"{x['symbol']} trạng thái {z['status']}")
        if z["rr1"] < config.MIN_RR:
            errs.append(f"{x['symbol']} R:R {z['rr1']:.2f} < {config.MIN_RR}")
        if not (z["zone_low"] * 0.995 <= f["price"] <= z["zone_high"] * 1.005):
            errs.append(f"{x['symbol']} giá ngoài vùng mua")
        if f["price"] <= f["ma50"]:
            errs.append(f"{x['symbol']} giá dưới MA50")
        if x["fd"] is None:
            errs.append(f"{x['symbol']} thiếu BCTC")
    p("➜ Kiểm tra điều kiện từng mã: " + ("✅ tất cả đúng" if not errs else "❌ " + "; ".join(errs)))

    trend, shortlist, scored = ctx["_trend"], ctx["_shortlist"], ctx["_scored"]
    beyond = [s for s in sorted(trend, key=lambda s: trend[s]["tech"], reverse=True) if s not in shortlist]
    miss_v1 = []
    for s in beyond:
        if s not in prices:
            continue
        z = an.compute_zones(prices[s], trend[s])
        if z["status"] == "MUA":
            miss_v1.append(f"{s}(KT {trend[s]['tech']:.0f})")
    p(f"Mã xu hướng tăng: {len(trend)} | được xét BCTC: {len(shortlist)} (giới hạn {config.FUND_CANDIDATES})")
    p(f"➜ Mã ĐANG Ở VÙNG MUA nhưng không được xét vì ngoài top {config.FUND_CANDIDATES} kỹ thuật: {len(miss_v1)}"
      + (f" – {', '.join(miss_v1)}" if miss_v1 else " ✅"))
    buy_all = [x for x in scored if x["z"]["status"] == "MUA" and x["fd"] is not None]
    cut = [x["symbol"] for x in buy_all if x["symbol"] not in {t["symbol"] for t in top1}]
    p(f"Mã MUA ĐƯỢC sau chấm điểm: {len(buy_all)} | bị loại do giới hạn 10 mã / {config.MAX_PER_SECTOR} mã mỗi ngành: "
      f"{len(cut)}" + (f" – {', '.join(cut)}" if cut else ""))

    # ------------------------------------------------------------------ 5: V2
    sec("4. BOT V2 – KIỂM TRA ĐIỀU KIỆN & MÃ BỊ SÓT")
    top2 = m2.run(dry=True, now=now)
    p(f"V2 đưa ra {len(top2)} mã: {', '.join(x['symbol'] for x in top2)}")
    ok, market, inds, rows = m2.LAST["ok"], m2.LAST["market"], m2.LAST["industries"], m2.LAST["rows"]
    no_fund = [r["symbol"] for r in rows if any(r.get(k) is None for k in ("margin", "roe", "pb"))]
    p(f"Mã thanh khoản: {len(rows)} | thiếu BCTC (biên LN/ROE/P/B) nên không chấm được: {len(no_fund)}"
      + (f" – {', '.join(no_fund[:40])}" if no_fund else " ✅"))
    periods = Counter(r["fd"]["period"] for r in rows if r.get("fd") and r["fd"].get("period"))
    p(f"Kỳ BCTC mới nhất đang dùng: {dict(periods.most_common(4))}")
    errs = []
    for x in top2:
        f = x["f"]
        if not (x["now_price"] > f["ma50"] and f["rsi"] >= 50):
            errs.append(f"{x['symbol']} không đạt giá>MA50 & RSI≥50")
        if x["rank"] > m2.SCORE_RANK_LIMIT:
            errs.append(f"{x['symbol']} hạng {x['rank']} > {m2.SCORE_RANK_LIMIT}")
    p("➜ Kiểm tra điều kiện từng mã: " + ("✅ tất cả đúng" if not errs else "❌ " + "; ".join(errs)))
    allbuy = []
    for rank, (_, r) in enumerate(ok.iterrows(), 1):
        d = r.to_dict()
        if m2.advise(d, market)["action"] == "MUA":
            allbuy.append((rank, d["symbol"], inds.get(d["symbol"], {}).get("sector", "Khác")))
    shown = {x["symbol"] for x in top2}
    in30 = [(rk, s, sc) for rk, s, sc in allbuy if rk <= m2.SCORE_RANK_LIMIT]
    cut2 = [f"{s}(hạng {rk})" for rk, s, sc in in30 if s not in shown]
    p(f"Mã ✅ trong top {m2.SCORE_RANK_LIMIT}: {len(in30)} | bị loại do giới hạn {m2.MAX_BUY} mã / "
      f"{m2.MAX_SECTOR_V2} mã mỗi ngành: {len(cut2)}" + (f" – {', '.join(cut2)}" if cut2 else ""))
    out30 = [f"{s}({rk})" for rk, s, _ in allbuy if m2.SCORE_RANK_LIMIT < rk <= 50]
    p(f"Mã ✅ hạng 31–50 (không hiện vì backtest cho thấy kém hơn): {', '.join(out30) if out30 else 'không có'}")

    # ------------------------------------------------------------------ kết luận
    sec("KẾT LUẬN")
    issues = []
    if len(vci.MISSING) > 10:
        issues.append(f"{len(vci.MISSING)} mã lỗi giá")
    if miss_uni:
        issues.append(f"{len(miss_uni)} mã thanh khoản chưa có trong danh sách lưu")
    if miss_v1:
        issues.append(f"V1 bỏ sót {len(miss_v1)} mã vùng mua do giới hạn 60 mã BCTC")
    if no_fund:
        issues.append(f"{len(no_fund)} mã thiếu BCTC")
    p("✅ Không phát hiện vấn đề" if not issues else "⚠️ Cần xem: " + " | ".join(issues))
    p(f"Thời gian chạy: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        p("")
        p("❌ LỖI KHI KIỂM TRA:")
        p(traceback.format_exc())
    finally:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
