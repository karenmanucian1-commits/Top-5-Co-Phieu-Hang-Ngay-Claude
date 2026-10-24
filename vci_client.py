"""
Lấy dữ liệu từ các API công khai của Vietcap (VCI) - cùng nguồn mà thư viện vnstock dùng.
Gọi trực tiếp bằng requests để:
  - tải giá của 40 mã trong 1 lần gọi (nhanh, ít bị chặn)
  - không phụ thuộc phiên bản vnstock thay đổi liên tục
"""
import json
import logging
import os
import pickle
import tempfile
import time

import pandas as pd
import requests
from datetime import datetime, timedelta

import config

log = logging.getLogger("vci")

TRADING_URL = "https://trading.vietcap.com.vn/api"
IQ_URL = "https://iq.vietcap.com.vn/api/iq-insight-service"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://trading.vietcap.com.vn/",
    "Origin": "https://trading.vietcap.com.vn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
}

INDEX_SYMBOLS = {"VNINDEX", "HNXINDEX", "UPCOMINDEX", "VN30", "HNX30"}

_session = requests.Session()
_session.headers.update(HEADERS)


def _request(method, url, retries=2, timeout=(10, 20), **kwargs):
    last_err = None
    for attempt in range(retries):
        try:
            r = _session.request(method, url, timeout=timeout, **kwargs)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("Bị giới hạn tốc độ (429), chờ %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Gọi API thất bại: {url} -> {last_err}")


# ----------------------------------------------------------------------------
# Danh sách mã & ngành
# ----------------------------------------------------------------------------
SYMBOLS_FILE = os.path.join(config.DATA_DIR, "symbols.json")
INDUSTRIES_FILE = os.path.join(config.DATA_DIR, "industries.json")


def _save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def get_all_symbols():
    """Trả về DataFrame: symbol, exchange (HSX/HNX/UPCOM).
    Lấy từ Vietcap; nếu Vietcap lỗi thì dùng bản đã lưu lần trước (data/symbols.json)."""
    try:
        data = _request("GET", f"{TRADING_URL}/price/symbols/getAll", timeout=(8, 15))
        df = pd.DataFrame(data)
        df = df.rename(columns={"board": "exchange"})
        if "type" in df.columns:
            df = df[df["type"].astype(str).str.upper() == "STOCK"]
        df = df[df["symbol"].astype(str).str.len() == 3]
        df["exchange"] = df["exchange"].astype(str).str.upper().replace({"HOSE": "HSX"})
        df = df[df["exchange"].isin(["HSX", "HNX", "UPCOM"])]
        name_col = "organName" if "organName" in df.columns else None
        df["name"] = df[name_col] if name_col else ""
        df = df[["symbol", "exchange", "name"]].drop_duplicates("symbol").reset_index(drop=True)
        if len(df) > 500:
            _save_json(SYMBOLS_FILE, df.to_dict("records"))
        return df
    except Exception as e:  # noqa: BLE001
        saved = _load_json(SYMBOLS_FILE)
        if saved:
            log.warning("Vietcap lỗi danh sách mã (%s) -> dùng bản đã lưu (%d mã)", str(e)[:80], len(saved))
            return pd.DataFrame(saved)
        raise


def get_industries():
    """
    Trả về dict: symbol -> {"sector": tên ngành cấp 2, "com_type": NH/CK/BH/CT}
    NH = ngân hàng, CK = chứng khoán, BH = bảo hiểm, CT = công ty thường.
    Vietcap lỗi thì dùng bản đã lưu (data/industries.json) – phân ngành gần như không đổi.
    """
    out = {}
    try:
        data = _request("GET", f"{IQ_URL}/v2/company/search-bar", params={"language": 1}, timeout=(8, 15))
        for c in data.get("data") or []:
            sym = c.get("code")
            if not sym:
                continue
            lv2 = (c.get("icbLv2") or {}).get("name")
            lv1 = (c.get("icbLv1") or {}).get("name")
            out[sym] = {
                "sector": lv2 or lv1 or "Khác",
                "com_type": (c.get("comTypeCode") or "CT").upper(),
                "name": c.get("name") or "",
            }
    except Exception as e:  # noqa: BLE001
        log.warning("Không lấy được phân ngành từ Vietcap: %s", str(e)[:80])
    if len(out) > 500:
        _save_json(INDUSTRIES_FILE, out)
        return out
    saved = _load_json(INDUSTRIES_FILE)
    if saved:
        log.warning("Dùng phân ngành đã lưu (%d mã)", len(saved))
        return saved
    return out


# ----------------------------------------------------------------------------
# Giá lịch sử (nhiều mã / 1 lần gọi)
# ----------------------------------------------------------------------------
def _parse_ohlc_item(item, symbol):
    if not isinstance(item, dict) or not isinstance(item.get("c"), list) or not item["c"]:
        return None
    df = pd.DataFrame({
        "t": item["t"], "open": item["o"], "high": item["h"],
        "low": item["l"], "close": item["c"], "volume": item["v"],
    })
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    df = df.dropna(subset=["t"])
    df["date"] = (
        pd.to_datetime(df["t"].astype("int64"), unit="s", utc=True)
        .dt.tz_convert("Asia/Ho_Chi_Minh").dt.tz_localize(None).dt.normalize()
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if symbol not in INDEX_SYMBOLS:
        # API trả giá theo đồng -> đổi sang nghìn đồng
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 1000.0
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    return df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)


_last_raw = {"sample": None}


def _iter_items(data, symbols):
    """API có thể trả list [{...}, ...] hoặc dict {"FPT": {...}} -> chuẩn hoá về (symbol, item)."""
    if isinstance(data, dict) and "data" in data and not isinstance(data.get("c"), list):
        data = data["data"]
    if isinstance(data, dict):
        if isinstance(data.get("c"), list):  # 1 mã, trả thẳng 1 object
            yield (data.get("symbol") or symbols[0]), data
            return
        for k, v in data.items():
            if isinstance(v, dict):
                yield (v.get("symbol") or k), v
        return
    if isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol") or item.get("ticker") or (symbols[i] if i < len(symbols) else None)
            yield sym, item


def _fetch_ohlc_batch(symbols, bars, fast=False):
    payload = {
        "timeFrame": "ONE_DAY",
        "symbols": list(symbols),
        "to": int(time.time()) + 86400,
        "countBack": int(bars),
    }
    kw = {"retries": 1, "timeout": (8, 10)} if fast else {}
    data = _request("POST", f"{TRADING_URL}/chart/OHLCChart/gap-chart", json=payload, **kw)
    _last_raw["sample"] = str(data)[:400]
    result = {}
    for sym, item in _iter_items(data, symbols):
        if not sym:
            continue
        try:
            df = _parse_ohlc_item(item, sym)
        except Exception as e:  # noqa: BLE001
            log.warning("Không đọc được giá %s: %s", sym, e)
            continue
        if df is not None and len(df):
            result[sym] = df
    return result


# ----------------------------------------------------------------------------
# Nguồn dự phòng: KBS (KB Securities) – dùng khi Vietcap chậm / không phản hồi
# ----------------------------------------------------------------------------
KBS_URL = "https://kbbuddywts.kbsec.com.vn/iis-server/investment"
_kbs = requests.Session()
_kbs.headers.update({k: v for k, v in HEADERS.items() if k not in ("Referer", "Origin")})
_kbs.headers.update({"Referer": "https://kbbuddy.kbsec.com.vn/", "Origin": "https://kbbuddy.kbsec.com.vn"})


def _fetch_kbs(symbol, bars):
    days = int(bars * 1.6) + 10
    end = datetime.now()
    start = end - timedelta(days=days)
    kind = "index" if symbol in INDEX_SYMBOLS else "stocks"
    r = _kbs.get(f"{KBS_URL}/{kind}/{symbol}/data_day",
                 params={"sdate": start.strftime("%d-%m-%Y"), "edate": end.strftime("%d-%m-%Y")},
                 timeout=(8, 15))
    r.raise_for_status()
    js = r.json()
    rows = js.get("data_day") if isinstance(js, dict) else None
    if not rows:
        return {}
    df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low",
                                            "c": "close", "v": "volume"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if symbol not in INDEX_SYMBOLS:
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 1000.0
    df = df.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last")
    df = df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").tail(bars).reset_index(drop=True)
    return {symbol: df} if len(df) else {}


SOURCE = None    # nguồn dùng cho CẢ lượt chạy: "VCI" (Vietcap) hoặc "KBS"
SOURCE_NAME = {"VCI": "Vietcap", "KBS": "KBS"}
PROBE = ["VNINDEX", "FPT", "VCB", "HPG", "MWG"]


def _fetch_src(src, symbol, bars, fast=True):
    if src == "KBS":
        return _fetch_kbs(symbol, bars)
    return _fetch_ohlc_batch([symbol], bars, fast=fast)


def _probe(src, bars):
    ok = 0
    for sym in PROBE:
        try:
            if _fetch_src(src, sym, min(bars, 30)):
                ok += 1
        except Exception:  # noqa: BLE001
            pass
    return ok


def choose_source(bars):
    """Chọn 1 nguồn cho cả lượt chạy: Vietcap trước, không ổn thì KBS."""
    global SOURCE
    if SOURCE:
        return SOURCE
    n = _probe("VCI", bars)
    if n >= 4:
        SOURCE = "VCI"
    else:
        k = _probe("KBS", bars)
        log.warning("Vietcap chỉ trả lời %d/%d mã thử, KBS %d/%d", n, len(PROBE), k, len(PROBE))
        if k < 3 and n < 3:
            raise RuntimeError("Cả Vietcap và KBS đều không phản hồi – thử lại sau.")
        SOURCE = "KBS" if k >= n else "VCI"
    log.info("Nguồn dữ liệu giá cho lượt chạy này: %s", SOURCE_NAME[SOURCE])
    return SOURCE


MISSING = []   # mã không tải được ở lần gọi get_ohlc gần nhất
_CACHE = os.path.join(tempfile.gettempdir(), "claude_top5_price_cache.pkl")


def _load_cache(bars):
    try:
        with open(_CACHE, "rb") as fh:
            c = pickle.load(fh)
        if time.time() - c["ts"] < 40 * 60 and c["bars"] >= bars:
            return c
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_cache(data, bars, src):
    try:
        old = _load_cache(bars)
        merged = old["data"] if old and old.get("src") == src else {}
        merged.update(data)
        with open(_CACHE, "wb") as fh:
            pickle.dump({"ts": time.time(), "bars": bars, "src": src, "data": merged}, fh)
    except Exception:  # noqa: BLE001
        pass


def get_ohlc(symbols, bars=None, max_minutes=20):
    """Tải giá ngày (1 mã/lần) từ MỘT nguồn duy nhất cho cả lượt chạy.
    - Chọn nguồn 1 lần: Vietcap; nếu Vietcap không ổn thì KBS.
    - Nếu Vietcap hỏng giữa chừng: chuyển sang KBS và TẢI LẠI TOÀN BỘ từ KBS (không trộn 2 nguồn).
    - Dùng lại giá vừa tải trong 40 phút (V1 tải xong thì V2 dùng lại).
    - Mã lỗi được thử lại 1 lần ở cuối; mã vẫn thiếu ghi vào MISSING để báo lên Telegram."""
    global SOURCE
    bars = bars or config.HISTORY_BARS
    symbols = list(symbols)
    c = _load_cache(bars)
    if c and c.get("src"):
        SOURCE = c["src"]          # bot V1 vừa tải xong -> V2 dùng đúng nguồn & dữ liệu đó
    if len(symbols) <= 5:
        c = None
    src = choose_source(bars)
    cached = c["data"] if c and c.get("src") == src else {}

    while True:
        out = {s: cached[s] for s in symbols if s in cached}
        todo = [s for s in symbols if s not in out]
        if cached:
            log.info("Dùng lại giá đã tải (%s): %d mã, cần tải thêm %d mã", SOURCE_NAME[src], len(out), len(todo))
        failed, streak, switched = [], 0, False
        t0 = time.time()
        for i, s in enumerate(todo, 1):
            try:
                got = _fetch_src(src, s, bars)
            except Exception:  # noqa: BLE001
                got = {}
            if got:
                out.update(got)
                streak = 0
            else:
                failed.append(s)
                streak += 1
            time.sleep(config.PRICE_SLEEP)
            if i % 50 == 0 or i == len(todo):
                log.info("Đã tải giá %d/%d mã từ %s (lỗi %d, %.0fs)", i, len(todo), SOURCE_NAME[src],
                         len(failed), time.time() - t0)
            if streak >= 5 and src == "VCI":
                log.warning("Vietcap hỏng giữa chừng -> chuyển sang KBS và tải lại toàn bộ")
                SOURCE = src = "KBS"
                cached = {}
                switched = True
                break
            if streak >= 10:
                raise RuntimeError(f"{SOURCE_NAME[src]} không phản hồi ({streak} mã liên tiếp lỗi) – thử lại sau.")
            if time.time() - t0 > max_minutes * 60:
                raise RuntimeError(f"Tải giá quá {max_minutes} phút (mới được {i}/{len(todo)} mã) – "
                                   f"nguồn dữ liệu đang chậm, thử lại sau.")
        if not switched:
            break

    if failed:
        log.info("Thử lại %d mã lỗi: %s", len(failed), ", ".join(failed[:20]))
        for s in list(failed):
            try:
                got = _fetch_src(src, s, bars, fast=False)
            except Exception:  # noqa: BLE001
                got = {}
            if got:
                out.update(got)
                failed.remove(s)
    MISSING.clear()
    MISSING.extend(failed)
    if failed:
        log.warning("Thiếu dữ liệu %d mã: %s", len(failed), ", ".join(failed))
    if len(symbols) > 5:
        _save_cache({s: out[s] for s in symbols if s in out}, bars, src)
    return out


# ----------------------------------------------------------------------------
# Chỉ số tài chính
# ----------------------------------------------------------------------------
def get_ratios(symbol):
    """Trả về list các bản ghi chỉ số tài chính theo kỳ (thô, từ VCI)."""
    data = _request("GET", f"{IQ_URL}/v1/company/{symbol}/statistics-financial")
    recs = data.get("data") if isinstance(data, dict) else data
    if isinstance(recs, dict):
        recs = [recs]
    return recs or []
