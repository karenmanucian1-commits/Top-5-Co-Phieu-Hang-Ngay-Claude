"""
Cấu hình CLAUDE BOT TOP5.
Muốn chỉnh ngưỡng lọc thì chỉ sửa file này.
"""

# ===================== LỚP 1: LỌC RÁC / THANH KHOẢN =====================
MIN_VALUE20_VND = 3_000_000_000   # GTGD bình quân 20 phiên tối thiểu: 3 tỷ/phiên
MIN_MEDIAN_VALUE20_VND = 1_500_000_000  # GTGD trung vị ≥1,5 tỷ (chống mã chỉ có vài phiên thoả thuận lớn)
MIN_TRADE_DAYS = 18               # Phải có giao dịch ít nhất 18/20 phiên gần nhất
MIN_PRICE_K = 5.0                 # Giá tối thiểu 5.000đ (đơn vị nghìn đồng)
MIN_BARS = 200                    # Cần ít nhất 200 phiên lịch sử (để có MA200)
MAX_STALE_DAYS = 7                # Dữ liệu cũ hơn 7 ngày -> loại

# ===================== LỚP 2: XU HƯỚNG BẮT BUỘC =====================
# Bot này đánh theo xu hướng tăng (khác bot "Vùng Đáy"):
# giá phải trên MA50 và MA50 không đi xuống.
REQUIRE_ABOVE_MA50 = True
MA50_SLOPE_LOOKBACK = 10          # so MA50 hiện tại với 10 phiên trước
MA50_SLOPE_MIN = -0.01            # MA50 không giảm quá 1%

# ===================== LỚP 3: TÀI CHÍNH BẮT BUỘC =====================
FUND_CANDIDATES = 60              # Chỉ lấy BCTC cho 60 mã kỹ thuật tốt nhất (giảm tải)
MIN_ROE_PCT = 5.0                 # ROE < 5% -> loại
REQUIRE_POSITIVE_EPS = True       # Đang lỗ (EPS <= 0 hoặc P/E <= 0) -> loại
FUND_CACHE_DAYS = 5               # BCTC ra theo quý, lưu cache 5 ngày

# ===================== VÙNG MUA / BÁN / CẮT LỖ =====================
ATR_PERIOD = 14
BUY_ZONE_ATR = 0.6                # Vùng mua = [hỗ trợ ; hỗ trợ + 0.6 ATR]
STOP_ATR = 1.0                    # Cắt lỗ = hỗ trợ - 1.0 ATR
MAX_RISK_PCT = 0.07               # Cắt lỗ không xa quá 7% so với giá mua
MIN_RISK_PCT = 0.03               # Cắt lỗ không gần quá 3% (tránh bị quét bởi dao động bình thường)
FEE_ROUNDTRIP = 0.004             # Phí mua + bán + thuế ~0.4%
MIN_RR = 1.5                      # R:R (sau phí) tối thiểu để được MUA
CHASE_LIMIT_PCT = 0.04            # Giá cao hơn vùng mua > 4% -> không mua đuổi
RSI_OVERHEAT = 75
# Mục tiêu chốt lời thực tế cho nắm giữ vài tuần
T1_MAX_PCT = 0.15                 # Mục tiêu 1 tối đa +15%
T2_MAX_PCT = 0.25                 # Mục tiêu 2 tối đa +25%

# ===================== CHẤM ĐIỂM & XẾP HẠNG =====================
W_TECH = 0.40
W_FUND = 0.35
W_SETUP = 0.25
TOP_N = 5
MAX_BUY_V1 = 10                   # V1: chỉ liệt kê mã MUA ĐƯỢC, tối đa 10 mã
MAX_PER_SECTOR = 2                # Tối đa 2 mã cùng ngành trong TOP5

# ===================== PHIÊN SÁNG =====================
# Lúc 11:30, khối lượng phiên sáng thường chiếm ~45% cả ngày.
# Dùng để quy đổi khối lượng phiên sáng ra cả ngày (tránh méo chỉ báo volume).
MORNING_VOLUME_FRACTION = 0.45

# ===================== DỮ LIỆU =====================
HISTORY_BARS = 320                # Số phiên lịch sử tải về
PRICE_SLEEP = 0.25                # Nghỉ giữa mỗi mã khi tải giá (giây)
REQUEST_SLEEP = 0.8               # Nghỉ giữa các lần gọi (giây) - lịch sự với nguồn dữ liệu
INDEX_SYMBOL = "VNINDEX"
# Danh sách mã có thanh khoản được lưu lại; quét lại toàn thị trường mỗi 7 ngày
UNIVERSE_REFRESH_DAYS = 7
UNIVERSE_MIN_VALUE20 = 1_500_000_000   # giữ mã ≥1,5 tỷ (thấp hơn ngưỡng 3 tỷ để có dự phòng)
DATA_DIR = "data"
