"""
Lấy dữ liệu từ các API công khai của Vietcap (VCI) - cùng nguồn mà thư viện vnstock dùng.
Gọi trực tiếp bằng requests để:
  - tải giá của 40 mã trong 1 lần gọi (nhanh, ít bị chặn)
  - không phụ thuộc phiên bản vnstock thay đổi liên tục
"""
import time
import logging

import pandas as pd
import requests

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


def _request(method, url, retries=3, **kwargs):
    last_err = None
    for attempt in range(retries):
        try:
            r = _session.request(method, url, timeout=30, **kwargs)
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
def get_all_symbols():
    """Trả về DataFrame: symbol, exchange (HSX/HNX/UPCOM)."""
    data = _request("GET", f"{TRADING_URL}/price/symbols/getAll")
    df = pd.DataFrame(data)
    df = df.rename(columns={"board": "exchange"})
    if "type" in df.columns:
        df = df[df["type"].astype(str).str.upper() == "STOCK"]
    df = df[df["symbol"].astype(str).str.len() == 3]
    df["exchange"] = df["exchange"].astype(str).str.upper().replace({"HOSE": "HSX"})
    df = df[df["exchange"].isin(["HSX", "HNX", "UPCOM"])]
    name_col = "organName" if "organName" in df.columns else None
    df["name"] = df[name_col] if name_col else ""
    return df[["symbol", "exchange", "name"]].drop_duplicates("symbol").reset_index(drop=True)


def get_industries():
    """
    Trả về dict: symbol -> {"sector": tên ngành cấp 2, "com_type": NH/CK/BH/CT}
    NH = ngân hàng, CK = chứng khoán, BH = bảo hiểm, CT = công ty thường.
    """
    out = {}
    try:
        data = _request("GET", f"{IQ_URL}/v2/company/search-bar", params={"language": 1})
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
        log.warning("Không lấy được phân ngành: %s", e)
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


def _fetch_ohlc_batch(symbols, bars):
    payload = {
        "timeFrame": "ONE_DAY",
        "symbols": list(symbols),
        "to": int(time.time()) + 86400,
        "countBack": int(bars),
    }
    data = _request("POST", f"{TRADING_URL}/chart/OHLCChart/gap-chart", json=payload)
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


def get_ohlc(symbols, bars=None):
    """Tải giá ngày từng mã (API chỉ trả dữ liệu khi hỏi 1 mã/lần). Trả về dict symbol -> DataFrame."""
    bars = bars or config.HISTORY_BARS
    out, fails = {}, 0
    symbols = list(symbols)
    for i, s in enumerate(symbols, 1):
        try:
            out.update(_fetch_ohlc_batch([s], bars))
        except Exception as e:  # noqa: BLE001
            fails += 1
            log.debug("Lỗi tải %s: %s", s, e)
        time.sleep(config.PRICE_SLEEP)
        if i % 200 == 0 or i == len(symbols):
            log.info("Đã tải giá %d/%d mã (có dữ liệu: %d, lỗi: %d)", i, len(symbols), len(out), fails)
        if i == 30 and not out:
            raise RuntimeError(f"30 mã đầu không tải được giá. Phản hồi API: {_last_raw['sample']}")
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
