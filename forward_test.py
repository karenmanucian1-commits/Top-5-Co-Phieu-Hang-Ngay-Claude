"""
FORWARD TEST – chấm điểm khuyến nghị THẬT của bot V1 và V2 bằng giá thật về sau.

  python forward_test.py            # in báo cáo ra màn hình
  python forward_test.py --send     # gửi báo cáo lên Telegram

Nguồn: data/picks_log.csv (V1) và data/picks_log_v2.csv (V2) – bot tự ghi mỗi ngày.
Chỉ chấm lượt 6:00 (dùng giá đóng cửa, session = CHIEU).

Quy tắc chấm (giống người mua thật):
  - Mua giá MỞ CỬA phiên ngay sau ngày khuyến nghị, phí mua+bán ~0.4%.
  - Nếu 1 mã được khuyến nghị nhiều ngày liền, chỉ tính LỆNH ĐẦU TIÊN; khuyến nghị lại sau khi lệnh cũ
    đã đủ 20 phiên mới tính là lệnh mới.
  - Theo dõi 20 phiên: lãi/lỗ sau 5 / 10 / 20 phiên, so với VN-Index cùng thời gian.
  - Theo kế hoạch của bot: từ phiên thứ 3 (T+2.5), giá cao nhất chạm mục tiêu 1 -> "Chạm chốt lời";
    giá đóng cửa dưới mức cắt lỗ -> "Dính cắt lỗ". Cái nào xảy ra trước tính cái đó.
  - Lệnh chưa đủ 20 phiên: "Đang theo dõi", chỉ tính lãi/lỗ tạm thời.
"""
import argparse
import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
import report
import vci_client as vci

D = config.DATA_DIR
LOGS = {"V1": os.path.join(D, "picks_log.csv"), "V2": os.path.join(D, "picks_log_v2.csv")}
HOLD = 20
FEE = config.FEE_ROUNDTRIP
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def load_log(path):
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    df = df[df["session"] == "CHIEU"].copy()
    df["run_date"] = pd.to_datetime(df["run_date"])
    return df.sort_values(["run_date", "rank"]).reset_index(drop=True)


def dedupe(df, trading_days):
    """Giữ lệnh đầu tiên của mỗi mã; khuyến nghị lại sau >= 20 phiên mới tính lệnh mới."""
    pos = {d: i for i, d in enumerate(trading_days)}
    keep, last = [], {}
    for i, r in df.iterrows():
        p = pos.get(r["run_date"])
        if p is None:
            continue
        s = r["symbol"]
        if s in last and p - last[s] < HOLD:
            continue
        last[s] = p
        keep.append(i)
    return df.loc[keep]


def evaluate_trade(r, px, idx):
    d = r["run_date"]
    after = px[px["date"] > d].reset_index(drop=True)
    if after.empty:
        return None
    entry = float(after["open"].iloc[0]) or float(after["close"].iloc[0])
    n = min(len(after), HOLD)
    seg = after.iloc[:n]
    out = {"bars": n, "entry_px": entry, "entry_date": seg["date"].iloc[0].date()}
    for h in (5, 10, 20):
        out[f"r{h}"] = (seg["close"].iloc[h - 1] / entry - 1 - FEE) * 100 if n >= h else np.nan
    out["r_now"] = (seg["close"].iloc[-1] / entry - 1 - FEE) * 100
    out["mfe"] = (seg["high"].max() / entry - 1) * 100
    out["mae"] = (seg["low"].min() / entry - 1) * 100

    # VN-Index cùng khoảng
    ia = idx[idx["date"] > d].reset_index(drop=True)
    if len(ia):
        ie = float(ia["open"].iloc[0]) or float(ia["close"].iloc[0])
        m = min(len(ia), n)
        out["vni_now"] = (ia["close"].iloc[m - 1] / ie - 1) * 100
        out["vni20"] = (ia["close"].iloc[HOLD - 1] / ie - 1) * 100 if len(ia) >= HOLD else np.nan

    # Kế hoạch chốt lời / cắt lỗ của bot
    t1 = r.get("t1")
    stop = r.get("stop")
    plan = "Chưa chạm"
    for j in range(2, n):
        if pd.notna(t1) and t1 and seg["high"].iloc[j] >= t1:
            plan = "Chạm chốt lời"
            break
        if pd.notna(stop) and stop and seg["close"].iloc[j] < stop:
            plan = "Dính cắt lỗ"
            break
    out["plan"] = plan
    out["closed"] = n >= HOLD
    return out


def stats(g, title):
    L = [f"<b>{title}</b>"]
    done = g[g["closed"]]
    opened = g[~g["closed"]]
    L.append(f"• Tổng {len(g)} lệnh | đủ 20 phiên: {len(done)} | đang theo dõi: {len(opened)}")
    if len(done):
        r = done["r20"]
        win, loss = r[r > 0], r[r <= 0]
        pf = win.sum() / -loss.sum() if loss.sum() < 0 else float("inf")
        L.append(f"• Lãi TB sau 20 phiên: <b>{r.mean():+.2f}%</b> (trung vị {r.median():+.2f}%) | "
                 f"VN-Index {done['vni20'].mean():+.2f}% | hơn VNI <b>{(r - done['vni20']).mean():+.2f}%</b>")
        L.append(f"• Tỷ lệ lãi: <b>{len(win) / len(r) * 100:.0f}%</b> ({len(win)} lãi / {len(loss)} lỗ) | "
                 f"lãi TB {win.mean() if len(win) else 0:+.1f}% · lỗ TB {loss.mean() if len(loss) else 0:+.1f}% | "
                 f"hệ số lãi/lỗ {pf:.2f}")
        for h in (5, 10):
            rh = done[f"r{h}"]
            L.append(f"• Sau {h} phiên: TB {rh.mean():+.2f}%, thắng {(rh > 0).mean() * 100:.0f}%")
        b, w = done.loc[r.idxmax()], done.loc[r.idxmin()]
        L.append(f"• Tốt nhất {b['symbol']} {b['r20']:+.1f}% | tệ nhất {w['symbol']} {w['r20']:+.1f}%")
        L.append(f"• Lên cao nhất TB +{done['mfe'].mean():.1f}% | xuống sâu nhất TB {done['mae'].mean():.1f}%")
    if len(g):
        vc = g["plan"].value_counts(normalize=True) * 100
        L.append("• Theo kế hoạch bot: " + " · ".join(f"{k} {v:.0f}%" for k, v in vc.items()))
    if len(opened):
        L.append(f"• Lệnh đang mở: lãi/lỗ tạm TB {opened['r_now'].mean():+.2f}% "
                 f"(VNI {opened['vni_now'].mean():+.2f}%)")
    if len(done) < 30:
        L.append("  <i>(chưa đủ 30 lệnh đóng – kết quả còn ít mẫu, chưa nên kết luận)</i>")
    return "\n".join(L)


def run(send=False, client=vci, now=None):
    now = now or datetime.now(VN_TZ)
    logs = {k: load_log(p) for k, p in LOGS.items()}
    syms = sorted(set().union(*[set(df["symbol"]) for df in logs.values() if len(df)]))
    if not syms:
        msg = "📒 FORWARD TEST: chưa có khuyến nghị nào được ghi nhật ký."
        print(msg)
        if send:
            report.send_telegram([msg])
        return None
    idx = client.get_ohlc([config.INDEX_SYMBOL], bars=150)[config.INDEX_SYMBOL]
    prices = client.get_ohlc(syms, bars=150)
    days = list(pd.to_datetime(idx["date"]))

    rows = []
    for bot, df in logs.items():
        if df.empty:
            continue
        df = df[df["status"] == "MUA"]          # chỉ chấm mã bot khuyến nghị NÊN MUA
        for _, r in dedupe(df, days).iterrows():
            px = prices.get(r["symbol"])
            if px is None:
                continue
            ev = evaluate_trade(r, px, idx)
            if ev:
                rows.append({"bot": bot, "run_date": r["run_date"].date(), "symbol": r["symbol"],
                             "status": r.get("status"), "priority": int(r.get("priority", 0) or 0), **ev})
    res = pd.DataFrame(rows)
    os.makedirs(D, exist_ok=True)
    res.to_csv(os.path.join(D, "forward_test.csv"), index=False, encoding="utf-8-sig")

    first = min(df["run_date"].min() for df in logs.values() if len(df)).date()
    parts = [f"📒 <b>FORWARD TEST – KHUYẾN NGHỊ THẬT</b>\n🕐 {now:%H:%M %d/%m/%Y} | từ {first:%d/%m/%Y}\n"
             "Mua giá mở cửa phiên sau khuyến nghị, đã trừ phí 0.4%, mỗi mã chỉ tính lệnh đầu tiên."]
    if res.empty:
        parts.append("Chưa có lệnh nào có giá sau ngày khuyến nghị.")
    else:
        for bot, icon in (("V1", "🤖"), ("V2", "💎")):
            g = res[res["bot"] == bot]
            if len(g):
                parts.append(stats(g, f"{icon} {bot} – mã NÊN MUA"))
                if g["priority"].sum():
                    parts.append(stats(g[g["priority"] == 1], f"{icon} {bot} – ⭐ Ưu tiên #1"))
        valid = res
        open_now = valid[~valid["closed"]].sort_values("r_now", ascending=False)
        if len(open_now):
            lines = [f"{r.bot} {r.symbol} ({r.run_date:%d/%m}): {r.r_now:+.1f}% sau {r.bars} phiên"
                     for r in open_now.head(12).itertuples()]
            parts.append("<b>Lệnh đang theo dõi</b>\n" + "\n".join(html.escape(x) for x in lines))
    msgs = report.split_messages(parts[0], parts[1:])
    for m in msgs:
        print(m)
    if send:
        report.send_telegram(msgs)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    run(send=ap.parse_args().send)
