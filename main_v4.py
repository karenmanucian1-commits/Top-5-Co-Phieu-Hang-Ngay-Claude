"""
CLAUDE BOT V4 – ÍT LỆNH, ƯU TIÊN THẮNG.  Chỉ nhắn Telegram khi CÓ TÍN HIỆU (mua hoặc bán).

Luật lấy NGUYÊN từ nghiên cứu research_v4.py (khóa trên 2023 → 04/2026, đạt kiểm tra 6 tháng sau):
  Chọn mã (dữ liệu đóng cửa phiên trước, chạy lượt 6:00):
    - Qua lọc thanh khoản như V1/V2 và GTGD TB20 >= 20 tỷ (mã lớn)
    - Xu hướng đủ: giá > MA20 > MA50 > MA200
    - Mạnh hơn VN-Index 60 phiên (tăng giá 60 phiên của mã > của VN-Index)
    - Đang chỉnh về gần MA20: giá <= MA20 + 0.5×ATR và giá > MA50
    - Nhiều mã đạt -> lấy 1 mã có điểm chất lượng V2 cao nhất
  Giao dịch: mua giá mở cửa phiên ngay sau tín hiệu; mỗi lúc chỉ 1 lệnh;
    chốt lời +5% (từ T+2.5); quá 20 phiên chưa chạm thì bán; không đặt cắt lỗ cứng.
    Lệnh mới chỉ khi đã qua 20 phiên kể từ tín hiệu trước (đúng như nghiên cứu).

Trạng thái lưu ở data/v4_state.json; nhật ký cho forward test ở data/picks_log_v4.csv.
"""
import contextlib
import html
import io
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import config
import main_v2
import report
import tracker
import vci_client as vci

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
STATE = os.path.join(config.DATA_DIR, "v4_state.json")
LOG_V4 = os.path.join(config.DATA_DIR, "picks_log_v4.csv")
BIG_VALUE20 = 20e9
TP = 0.05
HOLD = 20
FEE = config.FEE_ROUNDTRIP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("v4")


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"open": None, "last_signal": None, "history": []}


def save_state(st):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)


def fmt(x):
    return f"{x:,.2f}"


def check_open(pos, df, now):
    """Theo dõi lệnh đang mở. Trả về tin nhắn bán (hoặc None) và lệnh đã cập nhật."""
    sig = pd.Timestamp(pos["signal_date"])
    after = df[pd.to_datetime(df["date"]) > sig].reset_index(drop=True)
    if after.empty:
        return None, pos
    entry = pos.get("entry") or float(after["open"].iloc[0]) or float(after["close"].iloc[0])
    pos["entry"] = round(entry, 2)
    pos["entry_date"] = str(pd.Timestamp(after["date"].iloc[0]).date())
    tp_px = entry * (1 + TP)
    pos["tp"] = round(tp_px, 2)
    n = len(after)
    for j in range(2, min(n, HOLD)):                 # T+2.5: bán được từ phiên thứ 3
        if after["high"].iloc[j] >= tp_px:
            px = max(tp_px, float(after["open"].iloc[j]))
            ret = (px / entry - 1 - FEE) * 100
            d = pd.Timestamp(after["date"].iloc[j]).date()
            return (f"✅ <b>CLAUDE BOT V4 – CHỐT LỜI {pos['symbol']}</b>\n"
                    f"Giá chạm mốc +5% ({fmt(tp_px)}) phiên {d:%d/%m/%Y}.\n"
                    f"Mua {fmt(entry)} ngày {pd.Timestamp(pos['entry_date']):%d/%m} → bán {fmt(px)}: "
                    f"<b>{ret:+.1f}%</b> sau phí.\n"
                    "Nếu đã đặt lệnh bán chờ ở mốc +5% thì lệnh đã khớp."), dict(pos, closed="TP", exit=round(px, 2),
                                                                                   exit_date=str(d), ret=round(ret, 2))
    if n >= HOLD:
        px = float(after["close"].iloc[HOLD - 1])
        ret = (px / entry - 1 - FEE) * 100
        d = pd.Timestamp(after["date"].iloc[HOLD - 1]).date()
        return (f"⏰ <b>CLAUDE BOT V4 – HẾT 20 PHIÊN, BÁN {pos['symbol']}</b>\n"
                f"Chưa chạm mốc +5% sau 20 phiên. Bán hôm nay (giá đóng cửa {d:%d/%m}: {fmt(px)}).\n"
                f"Mua {fmt(entry)} → {fmt(px)}: <b>{ret:+.1f}%</b> sau phí."), dict(pos, closed="TIME", exit=round(px, 2),
                                                                              exit_date=str(d), ret=round(ret, 2))
    return None, pos


def pick(ok, rows, idx_df):
    """Tìm mã đạt luật V4, trả về mã có điểm chất lượng cao nhất (hoặc None)."""
    ic = idx_df["close"].astype(float)
    idx_ret60 = float(ic.iloc[-1] / ic.iloc[-61] - 1) if len(ic) > 60 else 0.0
    score = dict(zip(ok["symbol"], ok["score"]))
    cands = []
    for r in rows:
        f = r["f"]
        p, m20, m50, m200 = f["price"], f["ma20"], f["ma50"], f["ma200"]
        if r["symbol"] not in score or not m200 or f.get("ret60") is None:
            continue
        if (f["value20"] >= BIG_VALUE20 and p > m20 > m50 > m200 and f["ret60"] - idx_ret60 > 0
                and p <= m20 + 0.5 * f["atr"] and p > m50):
            cands.append({"symbol": r["symbol"], "score": score[r["symbol"]], "f": f, "rs60": (f["ret60"] - idx_ret60) * 100})
    log.info("Mã đạt luật V4: %d %s", len(cands), [c["symbol"] for c in cands])
    return max(cands, key=lambda c: c["score"]) if cands else None, len(cands)


def run(dry=False, now=None):
    now = now or datetime.now(VN_TZ)
    idx_df = vci.get_ohlc([config.INDEX_SYMBOL])[config.INDEX_SYMBOL]
    idx_last = pd.Timestamp(idx_df["date"].iloc[-1]).date()
    if idx_last == now.date() and (now.hour, now.minute) < (15, 0):
        log.info("Lượt trong phiên – V4 chỉ chạy lượt 6:00 (dữ liệu đóng cửa). Bỏ qua.")
        return
    # Dùng lại toàn bộ dữ liệu và điểm chất lượng của V2 (không gửi tin V2 lần nữa)
    with contextlib.redirect_stdout(io.StringIO()):
        main_v2.run(dry=True, now=now)
    L = main_v2.LAST
    st = load_state()
    msgs = []

    if st.get("open"):
        pos = st["open"]
        df = vci.get_ohlc([pos["symbol"]], bars=60)[pos["symbol"]]
        msg, pos = check_open(pos, df, now)
        if msg:
            msgs.append(msg)
            st["history"].append(pos)
            st["open"] = None
        else:
            st["open"] = pos

    # Lệnh mới: không có lệnh mở và đã qua 20 phiên kể từ tín hiệu trước
    dates = [pd.Timestamp(x).date() for x in idx_df["date"]]
    gap_ok = True
    if st.get("last_signal"):
        last = pd.Timestamp(st["last_signal"]).date()
        gap_ok = sum(1 for d in dates if d > last) >= HOLD
    if not st.get("open") and gap_ok:
        best, n = pick(L["ok"], L["rows"], idx_df)
        if best:
            f = best["f"]
            p = f["price"]
            msgs.append(
                f"🎯 <b>CLAUDE BOT V4 – TÍN HIỆU MUA {best['symbol']}</b>\n"
                f"🕐 {now:%H:%M %d/%m/%Y} | dữ liệu đóng cửa {idx_last:%d/%m}\n"
                f"💰 Giá đóng cửa: <b>{fmt(p)}</b> – mua giá mở cửa hôm nay (khoảng {fmt(p * 0.99)} – {fmt(p * 1.01)})\n"
                f"🎯 Chốt lời: <b>giá mua × 1.05</b> (≈ {fmt(p * 1.05)}). Từ T+2.5 đặt lệnh bán chờ ở mức này.\n"
                f"⏰ Chưa chạm sau 20 phiên thì bán. Không đặt cắt lỗ cứng.\n"
                f"📊 Lý do: GTGD {f['value20'] / 1e9:,.0f} tỷ/phiên · giá > MA20 > MA50 > MA200 · "
                f"mạnh hơn VN-Index {best['rs60']:+.1f}% (60 phiên) · đang chỉnh về MA20 ({fmt(f['ma20'])}) · "
                f"điểm chất lượng {best['score']:.0f}/100" + (f" · {n} mã đạt, chọn mã tốt nhất" if n > 1 else "") + "\n"
                f"📈 Kiểm chứng 2023–2026: thắng 73% (37 lệnh), lãi TB +1,1%/lệnh, lệnh lỗ nặng nhất −12%. "
                f"Chỉ nên vào tỷ trọng vừa phải.")
            st["open"] = {"symbol": best["symbol"], "signal_date": str(idx_last), "signal_price": round(p, 2),
                          "score": round(best["score"], 1)}
            st["last_signal"] = str(idx_last)
            if not dry:
                tracker.log_picks([[str(idx_last), "CHIEU", 1, best["symbol"], round(p, 2), round(p, 2), None,
                                    round(p * 1.05, 2), "MUA", 1, round(best["score"], 1)]], path=LOG_V4)

    st["last_run"] = now.strftime("%Y-%m-%d %H:%M")
    if dry:
        print("\n\n".join(msgs) if msgs else "(không có tín hiệu – không gửi tin)")
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    save_state(st)
    if msgs:
        report.send_telegram(msgs)
    else:
        log.info("Không có tín hiệu – không gửi Telegram")


def main():
    dry = "--dry" in sys.argv
    try:
        run(dry=dry)
    except Exception as e:  # noqa: BLE001
        log.exception("Bot V4 lỗi")
        if not dry:
            try:
                report.send_telegram([f"⚠️ CLAUDE BOT V4 lỗi: {html.escape(str(e)[:500])}"])
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
