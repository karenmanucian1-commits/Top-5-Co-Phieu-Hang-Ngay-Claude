"""Soạn tin nhắn Telegram (HTML) và gửi."""
import html
import logging
import os
import time

import requests

import config
from analysis import STATUS_TEXT

log = logging.getLogger("report")


def _p(x):
    return "N/A" if x is None else f"{x:,.2f}"


def _pct(x, sign=True):
    if x is None:
        return "N/A"
    return f"{x:+.1f}%" if sign else f"{x:.1f}%"


def _comment(s):
    f, fd, z = s["f"], s["fd"], s["z"]
    pros, cons = [], []
    if f["ma200"] and f["price"] > f["ma20"] > f["ma50"] > f["ma200"]:
        pros.append("xu hướng tăng đầy đủ (giá > MA20 > MA50 > MA200)")
    if f.get("rs_pct20", 0) >= 0.8:
        pros.append(f"mạnh hơn {f['rs_pct20'] * 100:.0f}% số mã trên thị trường (20 phiên)")
    if f["updown_vol"] >= 1.3:
        pros.append("dòng tiền vào rõ (KL phiên tăng > phiên giảm)")
    if z["breakout"]:
        pros.append("đang ở vùng đỉnh mới, phía trên không còn kháng cự cũ")
    if fd:
        if fd["roe"] is not None and fd["roe"] >= 15:
            pros.append(f"ROE cao {fd['roe']:.1f}%")
        if fd["eps_growth"] is not None and fd["eps_growth"] >= 15:
            pros.append(f"lợi nhuận 4 quý tăng {fd['eps_growth']:.0f}% so cùng kỳ")
        if fd["pe"] is not None and 0 < fd["pe"] <= 10:
            pros.append(f"định giá rẻ (P/E {fd['pe']:.1f})")
        if fd["eps_growth"] is not None and fd["eps_growth"] < 0:
            cons.append(f"lợi nhuận 4 quý giảm {abs(fd['eps_growth']):.0f}% so cùng kỳ")
        if fd["pe"] is not None and fd["pe"] > 25:
            cons.append(f"P/E cao ({fd['pe']:.1f})")
        if s["com_type"] not in ("NH", "CK", "BH") and fd["de"] is not None and fd["de"] > 2:
            cons.append(f"nợ vay cao (Nợ vay/VCSH {fd['de']:.1f})")
    else:
        cons.append("thiếu dữ liệu BCTC – chỉ đánh giá kỹ thuật")
    if f["rsi"] > 70:
        cons.append(f"RSI cao {f['rsi']:.0f}, dễ rung lắc")
    if z["rr1"] < config.MIN_RR:
        cons.append(f"kháng cự gần, R:R mục tiêu 1 chỉ {z['rr1']:.1f}")
    if z["stop_capped"]:
        cons.append("biến động mạnh, cắt lỗ bị giới hạn 7% – nên giảm tỷ trọng")
    return pros[:3], cons[:3]


def build_message(ctx):
    m = ctx["market"]
    lines = []
    lines.append(f"🤖 <b>CLAUDE BOT TOP5</b> – {ctx['session_label']}")
    src = f"  |  nguồn giá: {ctx['source']}" if ctx.get("source") else ""
    lines.append(f"🕐 {ctx['run_time']}  |  {ctx['data_note']}{src}")
    lines.append("")
    lines.append(f"📊 <b>THỊ TRƯỜNG: {m['regime']}</b>")
    above50 = "trên" if m["price"] > m["ma50"] else "dưới"
    above200 = "trên" if m["price"] > m["ma200"] else "dưới"
    lines.append(f"• VN-Index {m['price']:,.2f} ({_pct(m['ret20'])} 20 phiên) – {above50} MA50, {above200} MA200")
    lines.append(f"• Độ rộng: {m['breadth']:.0f}% mã thanh khoản tốt nằm trên MA50")
    lines.append(f"• Tỷ trọng gợi ý: {m['exposure']}")
    lines.append("")
    fn = ctx["funnel"]
    lines.append(
        f"🔎 Quét {fn['all']:,} mã → {fn['liquid']} mã GTGD ≥3 tỷ → {fn['trend']} mã xu hướng tăng "
        f"→ {fn['fund']} mã phân tích BCTC → {fn['passed']} mã đạt"
    )
    lines.append("")

    miss = ctx.get("missing") or []
    if miss:
        lines.append(f"⚠️ Thiếu dữ liệu {len(miss)} mã (nguồn dữ liệu lỗi): {', '.join(miss[:15])}")
        lines.append("")
    top = ctx["top"]
    if not top:
        lines.append("⚠️ Hôm nay không có cổ phiếu nào đủ điều kiện mua – đứng ngoài, giữ tiền.")
        return "\n".join(lines), []

    lines.append(f"✅ <b>CỔ PHIẾU NÊN MUA ({len(top)} mã):</b> " + " · ".join(s["symbol"] for s in top))
    pr = ctx["priority"]
    if pr:
        lines.append(f"⭐ <b>ƯU TIÊN #1: {pr['symbol']}</b> – mua trong vùng {_p(pr['z']['zone_low'])}–{_p(pr['z']['zone_high'])}, cắt lỗ {_p(pr['z']['stop'])}")
        if m["points"] <= 1:
            lines.append("   ⚠️ Thị trường đang xấu: chỉ mua thăm dò tối đa 20% vốn")
    else:
        lines.append("⭐ <b>ƯU TIÊN #1: CHƯA CÓ</b> – không mã nào đang ở điểm mua đẹp, kiên nhẫn chờ giá về vùng mua")

    perf = ctx.get("perf") or []
    if perf:
        lines.append("")
        lines.append("📒 <b>Hiệu quả thật các lần chọn trước</b>")
        for p in perf:
            t = (f"• TOP5 ngày {p['date']} (sau {p['n']} phiên): TB {_pct(p['avg'])}, "
                 f"thắng {p['win']}/{p['total']} | VN-Index {_pct(p['idx'])}")
            if p["priority"]:
                t += f" | Ưu tiên #1 {p['priority'][0]} {_pct(p['priority'][1])}"
            lines.append(t)

    header = "\n".join(lines)

    blocks = []
    for i, s in enumerate(top, 1):
        f, fd, z = s["f"], s["fd"], s["z"]
        star = " ⭐" if pr and s["symbol"] == pr["symbol"] else ""
        b = []
        b.append("━━━━━━━━━━━━━━")
        b.append(f"<b>#{i} {s['symbol']}</b>{star} · {s['exchange']} · {html.escape(s['sector'])}")
        b.append(f"Điểm tổng <b>{s['final']:.1f}</b>  (Kỹ thuật {s['tech']:.0f} | Tài chính {s['fund']:.0f} | Điểm mua {s['setup']:.0f})")
        chg = s.get("chg_pct")
        b.append(f"💰 Giá: <b>{_p(f['price'])}</b>" + (f" ({_pct(chg)})" if chg is not None else ""))
        b.append(f"<b>{STATUS_TEXT[z['status']]}</b>")
        b.append(f"🟢 Vùng mua: <b>{_p(z['zone_low'])} – {_p(z['zone_high'])}</b>")
        up1 = (z["t1"] / z["entry"] - 1) * 100
        up2 = (z["t2"] / z["entry"] - 1) * 100
        b.append(f"🎯 Vùng bán (chốt lời): <b>{_p(z['t1'])} – {_p(z['t2'])}</b> ({_pct(up1)} / {_pct(up2)})")
        b.append(f"🛑 Cắt lỗ: <b>{_p(z['stop'])}</b> (−{z['risk_pct']:.1f}% từ giá mua {_p(z['entry'])})")
        b.append(f"⚖️ R:R sau phí: {z['rr1']:.1f} (mục tiêu 1) | {z['rr2']:.1f} (mục tiêu 2)")
        if f["price"] >= z["t1"]:
            b.append("📤 Nếu đang cầm: giá đã vào vùng chốt lời – cân nhắc chốt 1/2, dời cắt lỗ lên MA20")
        rs20 = f.get("rs20")
        b.append(
            f"📈 KT: RSI {f['rsi']:.0f} · ADX {f['adx']:.0f} · KL {f['vol_ratio']:.1f}x TB20 · "
            f"GTGD {f['value20'] / 1e9:,.1f} tỷ/phiên · RS20 {_pct(rs20 * 100 if rs20 is not None else None)} vs VNI"
        )
        if fd:
            parts = []
            if fd["roe"] is not None:
                parts.append(f"ROE {fd['roe']:.1f}%")
            if fd["eps_growth"] is not None:
                parts.append(f"LNST 4 quý {_pct(fd['eps_growth'])} YoY")
            if fd["pe"] is not None:
                parts.append(f"P/E {fd['pe']:.1f}")
            if fd["pb"] is not None:
                parts.append(f"P/B {fd['pb']:.1f}")
            if s["com_type"] not in ("NH", "CK", "BH"):
                if fd["de"] is not None:
                    parts.append(f"Nợ vay/VCSH {fd['de']:.2f}")
                if fd["margin"] is not None:
                    parts.append(f"Biên LN {fd['margin']:.1f}%")
            per = f" ({fd['period']})" if fd.get("period") else ""
            b.append(f"🏢 TC{per}: " + " · ".join(parts))
        else:
            b.append("🏢 TC: không lấy được dữ liệu BCTC")
        pros, cons = _comment(s)
        if pros:
            b.append("👍 " + "; ".join(pros))
        if cons:
            b.append("👎 " + "; ".join(cons))
        blocks.append("\n".join(b))

    footer = (
        "━━━━━━━━━━━━━━\n"
        "📌 <b>Cách dùng:</b> chỉ mua khi giá nằm trong vùng mua, chia 2 lần. "
        "Tới vùng bán: chốt 1/2, phần còn lại dời cắt lỗ lên giá vốn. "
        "Đóng cửa dưới mức cắt lỗ → bán, không gồng. Nhớ T+2.5: mua xong ~3 ngày mới bán được.\n"
        "<i>Bot tham khảo, không phải khuyến nghị đầu tư.</i>"
    )
    return header, blocks + [footer]


def split_messages(header, blocks, limit=3900):
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    msgs.append(cur)
    return msgs


def send_telegram(messages):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Chưa có TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -> chỉ in ra màn hình")
        for m in messages:
            print(m)
            print("-" * 40)
        return False
    for m in messages:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": m, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            log.error("Gửi Telegram lỗi: %s", r.text[:300])
            r.raise_for_status()
        time.sleep(1)
    return True
