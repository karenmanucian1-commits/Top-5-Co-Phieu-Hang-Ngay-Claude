"""
CLAUDE BOT TOP5 – chọn 5 cổ phiếu tốt nhất mỗi ngày (kỹ thuật + tài chính + điểm mua).

Chạy:  python main.py            (gửi Telegram nếu có token)
       python main.py --dry      (chỉ in ra màn hình, không gửi, không ghi log)
"""
import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import analysis as an
import config
import report
import tracker
import vci_client as vci

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CACHE_PATH = os.path.join(config.DATA_DIR, "fund_cache.json")
UNIVERSE_PATH = os.path.join(config.DATA_DIR, "universe.json")


def load_universe(now):
    """Danh sách mã thanh khoản đã lưu (nếu còn mới) -> đỡ phải tải giá cả 1.500 mã mỗi lần."""
    try:
        with open(UNIVERSE_PATH, encoding="utf-8") as fh:
            u = json.load(fh)
        age = (now.date() - datetime.fromisoformat(u["date"]).date()).days
        if age < config.UNIVERSE_REFRESH_DAYS and len(u["symbols"]) >= 50:
            return u["symbols"]
    except Exception:  # noqa: BLE001
        pass
    return None


def save_universe(now, symbols):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(UNIVERSE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"date": now.date().isoformat(), "symbols": sorted(symbols)}, fh)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")


# ----------------------------------------------------------------------------
def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_cache(cache):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    cutoff = (datetime.now(VN_TZ) - timedelta(days=30)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("ts", "") >= cutoff}
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)


def get_ratios_cached(symbol, cache, now, client=vci):
    item = cache.get(symbol)
    if item:
        age = now - datetime.fromisoformat(item["ts"])
        if age.days < config.FUND_CACHE_DAYS:
            return item["recs"]
    try:
        recs = client.get_ratios(symbol)
        cache[symbol] = {"ts": now.isoformat(), "recs": recs}
        time.sleep(config.REQUEST_SLEEP)
        return recs
    except Exception as e:  # noqa: BLE001
        log.warning("Không lấy được BCTC %s: %s", symbol, e)
        return item["recs"] if item else None


# ----------------------------------------------------------------------------
def run(dry=False, client=vci, now=None):
    now = now or datetime.now(VN_TZ)
    today = now.date()

    # ---------- 1. Dữ liệu ----------
    symbols_df = client.get_all_symbols()
    industries = client.get_industries()
    log.info("Tổng số mã: %d", len(symbols_df))

    idx_map = client.get_ohlc([config.INDEX_SYMBOL])
    idx_df = idx_map.get(config.INDEX_SYMBOL)
    if idx_df is None or len(idx_df) < 60:
        raise RuntimeError("Không tải được VN-Index")

    idx_last = pd.Timestamp(idx_df["date"].iloc[-1]).date()
    intraday = idx_last == today and now.weekday() < 5 and (now.hour, now.minute) < (15, 0)
    if intraday:
        session, session_label = "SANG", f"PHIÊN SÁNG {today:%d/%m/%Y}"
        data_note = "dữ liệu phiên sáng, KL đã quy đổi ra cả ngày"
        mins_pm = (now.hour - 13) * 60 + now.minute
        if mins_pm > 0:  # lỡ chạy trễ vào phiên chiều: tăng tỷ lệ quy đổi theo thời gian
            config.MORNING_VOLUME_FRACTION = min(1.0, 0.45 + 0.55 * mins_pm / 105)
            session_label = f"TRONG PHIÊN {now:%H:%M} {today:%d/%m/%Y}"
            data_note = "dữ liệu đang giao dịch, KL đã quy đổi ra cả ngày"
    else:
        session = "CHIEU"  # dữ liệu đã đóng cửa trọn phiên -> dùng để ghi nhật ký hiệu quả
        if idx_last < today:
            session_label = f"TRƯỚC PHIÊN {today:%d/%m/%Y}"
            data_note = f"tham chiếu giá đóng cửa phiên {idx_last:%d/%m}"
        else:
            session_label = f"CUỐI NGÀY {idx_last:%d/%m/%Y}"
            data_note = f"dữ liệu đóng cửa {idx_last:%d/%m}"

    all_syms = symbols_df["symbol"].tolist()
    cached = load_universe(now)
    full_scan = cached is None
    fetch_syms = all_syms if full_scan else [s for s in cached if s in set(all_syms)]
    log.info("%s: tải giá %d mã", "Quét toàn thị trường" if full_scan else "Dùng danh sách thanh khoản đã lưu",
             len(fetch_syms))
    price_map = client.get_ohlc(fetch_syms)
    log.info("Tải được giá %d mã", len(price_map))

    # ---------- 2. Lọc thanh khoản + xu hướng ----------
    def idx_ret(n):
        c = idx_df["close"]
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1)

    ir20, ir60 = idx_ret(20), idx_ret(60)
    meta = symbols_df.set_index("symbol").to_dict("index")

    liquid, universe = {}, []
    for sym, df in price_map.items():
        partial = intraday and pd.Timestamp(df["date"].iloc[-1]).date() == today
        try:
            f, why = an.technical_features(df, partial)
        except Exception as e:  # noqa: BLE001
            log.warning("Lỗi tính %s: %s", sym, e)
            continue
        if f is None:
            continue
        if f["value20"] >= config.UNIVERSE_MIN_VALUE20:
            universe.append(sym)
        if an.liquidity_filter(f, today):
            continue
        f["partial"] = partial
        f["prev_close"] = float(df["close"].iloc[-2])
        f["rs20"] = (f["ret20"] - ir20) if f["ret20"] is not None else None
        f["rs60"] = (f["ret60"] - ir60) if f["ret60"] is not None else None
        liquid[sym] = f

    if not liquid:
        raise RuntimeError("Không có mã nào qua lọc thanh khoản - kiểm tra lại dữ liệu")
    if full_scan and len(universe) >= 50:
        save_universe(now, universe)

    # Sức mạnh tương đối: xếp hạng phần trăm trong nhóm thanh khoản tốt
    rs = pd.DataFrame({s: {"rs20": f["rs20"], "rs60": f["rs60"]} for s, f in liquid.items()}).T
    rs = rs.astype(float)
    pct20 = rs["rs20"].rank(pct=True)
    pct60 = rs["rs60"].rank(pct=True)
    for s, f in liquid.items():
        f["rs_pct20"] = float(pct20.get(s, 0.5)) if pd.notna(pct20.get(s)) else 0.5
        f["rs_pct60"] = float(pct60.get(s, 0.5)) if pd.notna(pct60.get(s)) else 0.5

    breadth = sum(1 for f in liquid.values() if f["price"] > f["ma50"]) / len(liquid) * 100
    market = an.market_regime(idx_df, breadth)

    trend = {}
    for s, f in liquid.items():
        if an.trend_filter(f):
            continue
        f["tech"], f["tech_detail"] = an.technical_score(f)
        trend[s] = f
    log.info("Thanh khoản: %d | Xu hướng tăng: %d", len(liquid), len(trend))

    # ---------- 3. Tài chính cho nhóm kỹ thuật tốt nhất ----------
    shortlist = sorted(trend, key=lambda s: trend[s]["tech"], reverse=True)[: config.FUND_CANDIDATES]
    cache = load_cache()
    scored = []
    for s in shortlist:
        f = trend[s]
        ind_info = industries.get(s, {})
        com_type = ind_info.get("com_type", "CT")
        recs = get_ratios_cached(s, cache, now, client)
        try:
            fd = an.parse_ratios(recs)
        except Exception as e:  # noqa: BLE001
            log.warning("Không đọc được BCTC %s: %s", s, e)
            fd = None
        if an.fundamental_filter(fd):
            continue
        fund, _ = an.fundamental_score(fd, com_type)
        z = an.compute_zones(price_map[s], f)
        setup = an.setup_score(z, f)
        final = config.W_TECH * f["tech"] + config.W_FUND * fund + config.W_SETUP * setup
        scored.append({
            "symbol": s, "exchange": meta.get(s, {}).get("exchange", ""),
            "sector": ind_info.get("sector", "Khác"), "com_type": com_type,
            "f": f, "fd": fd, "z": z, "tech": f["tech"], "fund": fund, "setup": setup,
            "final": round(final, 1),
            "chg_pct": (f["price"] / f["prev_close"] - 1) * 100 if f["prev_close"] else None,
        })
    if not dry:
        save_cache(cache)

    # ---------- 4. Xếp hạng TOP5 (tối đa 2 mã/ngành) ----------
    scored.sort(key=lambda x: x["final"], reverse=True)
    # Chỉ giữ mã MUA ĐƯỢC (giá trong vùng mua, R:R đạt, có BCTC), tối đa 10 mã, 2 mã/ngành
    top, per_sector = [], {}
    for x in scored:
        if x["z"]["status"] != "MUA" or x["fd"] is None:
            continue
        if per_sector.get(x["sector"], 0) >= config.MAX_PER_SECTOR:
            continue
        top.append(x)
        per_sector[x["sector"]] = per_sector.get(x["sector"], 0) + 1
        if len(top) >= config.MAX_BUY_V1:
            break

    # Ưu tiên #1: điểm cao nhất trong số mã ĐANG mua được, có BCTC
    buyable = [x for x in top if x["z"]["status"] == "MUA" and x["fd"] is not None]
    priority = max(buyable, key=lambda x: x["final"]) if buyable else None
    if priority and market["points"] <= 1 and priority["setup"] < 70:
        priority = None  # thị trường xấu: chỉ chọn khi điểm mua thật đẹp

    # ---------- 5. Theo dõi hiệu quả & báo cáo ----------
    perf = tracker.evaluate(price_map, idx_df) if session == "CHIEU" else []

    ctx = {
        "session_label": session_label,
        "run_time": now.strftime("%H:%M %d/%m/%Y"),
        "data_note": data_note,
        "market": market,
        "funnel": {"all": len(symbols_df), "scanned": len(price_map), "liquid": len(liquid), "trend": len(trend),
                   "fund": len(shortlist), "passed": len(scored)},
        "top": top, "priority": priority, "perf": perf,
        "_trend": trend, "_shortlist": shortlist, "_scored": scored, "_liquid": liquid,
        "missing": list(getattr(client, "MISSING", [])),
        "source": getattr(client, "SOURCE_NAME", {}).get(getattr(client, "SOURCE", None), ""),
    }
    header, blocks = report.build_message(ctx)
    msgs = report.split_messages(header, blocks)

    if dry:
        for m in msgs:
            print(m)
            print("-" * 40)
    else:
        report.send_telegram(msgs)
        if top:
            run_date = idx_last.strftime("%Y-%m-%d")
            tracker.log_picks([
                [run_date, session, i, x["symbol"], round(x["f"]["price"], 2),
                 round(x["z"]["entry"], 2), round(x["z"]["stop"], 2), round(x["z"]["t1"], 2),
                 x["z"]["status"], int(priority is not None and x["symbol"] == priority["symbol"]),
                 x["final"]]
                for i, x in enumerate(top, 1)
            ])
    return ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="chỉ in, không gửi Telegram, không ghi log")
    args = ap.parse_args()
    try:
        run(dry=args.dry)
    except Exception as e:  # noqa: BLE001
        log.exception("Bot lỗi")
        if not args.dry:
            try:
                report.send_telegram([f"⚠️ CLAUDE BOT TOP5 lỗi: {html.escape(str(e)[:500])}"])
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
