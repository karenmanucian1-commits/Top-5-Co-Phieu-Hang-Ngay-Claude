# CLAUDE BOT TOP5 – 5 cổ phiếu tốt nhất mỗi ngày

Bot quét toàn bộ cổ phiếu HSX, HNX và UPCOM, lọc nhiều lớp, rồi chấm điểm theo 3 trụ:
**Kỹ thuật 40% + Tài chính doanh nghiệp 35% + Điểm mua 25%**.
Kết quả gửi về Telegram **2 lần/ngày**:

- **11:35** (sau phiên sáng): dùng dữ liệu phiên sáng, khối lượng được quy đổi ra cả ngày.
- **15:15** (sau đóng cửa): dùng giá đóng cửa. Chỉ lượt này được ghi vào nhật ký để chấm hiệu quả.

---

## 1. Logic chọn mã

```
~1.500 mã HSX/HNX/UPCOM
   │
   ▼  LỚP 1 – LỌC RÁC
   │  GTGD bình quân 20 phiên ≥ 3 tỷ, có giao dịch ≥ 18/20 phiên,
   │  giá ≥ 5.000đ, có ≥ 200 phiên lịch sử, dữ liệu không cũ
   ▼  LỚP 2 – XU HƯỚNG BẮT BUỘC
   │  Giá > MA50 và MA50 không đi xuống
   ▼  CHẤM ĐIỂM KỸ THUẬT (0-100)
   │  Cấu trúc xu hướng 25 · Sức mạnh so với VN-Index 25 · Động lượng 20
   │  Dòng tiền 15 · Độ mạnh xu hướng ADX 15
   ▼  60 mã kỹ thuật tốt nhất → LỚP 3 – TÀI CHÍNH
   │  Loại mã đang lỗ (EPS ≤ 0) và mã có ROE < 5%
   │  Chấm điểm: ROE 25 · Tăng trưởng EPS 25 · P/E 20 · Nợ/VCSH 15 · Biên LN 15
   │  (Ngân hàng/CK/bảo hiểm không chấm nợ & biên LN như DN thường)
   ▼  VÙNG MUA / BÁN / CẮT LỖ + ĐIỂM MUA (0-100)
   │  Hỗ trợ gần nhất (MA20, MA50, đáy gần) → vùng mua = hỗ trợ + 0.6 ATR
   │  Cắt lỗ = hỗ trợ − 1 ATR (tối đa 7%)
   │  Vùng bán = kháng cự gần nhất → kháng cự kế tiếp
   │  R:R tính SAU PHÍ (~0.4% khứ hồi)
   ▼  ĐIỂM TỔNG = 0.40 KT + 0.35 TC + 0.25 Điểm mua
   │  Tối đa 2 mã cùng ngành
   ▼
🏆 TOP 5  →  ⭐ ƯU TIÊN #1 = mã điểm cao nhất ĐANG ở vùng mua và R:R ≥ 1.5
             (không có mã nào đạt → bot nói thẳng "CHƯA CÓ", không ép mua)
```

**Trạng thái từng mã:**

| Trạng thái | Ý nghĩa |
|---|---|
| ✅ MUA ĐƯỢC | Giá đang trong vùng mua, R:R đạt |
| ⚠️ R:R chưa đạt | Trong vùng mua nhưng kháng cự quá gần |
| ⏳ CHỜ | Cao hơn vùng mua dưới 4%, chờ điều chỉnh |
| 🚫 Không mua đuổi | Đã chạy xa vùng mua hoặc RSI > 75 |
| ⚠️ / ❌ | Thủng hỗ trợ hoặc thủng mức cắt lỗ |

**Thị trường:** bot xét VN-Index so với MA50/MA200 và tỷ lệ mã nằm trên MA50, rồi gợi ý tỷ trọng giải ngân.
Khi thị trường XẤU, bot chỉ chọn Ưu tiên #1 nếu điểm mua thật đẹp.

**Tự chấm hiệu quả:** mỗi ngày bot ghi TOP5 vào `data/picks_log.csv`.
Sau 5 và 20 phiên, báo cáo tự in lãi/lỗ thật của TOP5 cũ so với VN-Index.

Muốn chỉnh ngưỡng nào thì chỉ sửa file **`config.py`**.

---

## 2. Cài đặt trên GitHub (chạy tự động, không cần bật máy)

1. Tạo repo **private** mới trên GitHub, ví dụ `Claude-Bot-Top5`.
2. Upload toàn bộ thư mục này lên repo, nhớ có cả thư mục `.github`.
3. Vào **Settings → Secrets and variables → Actions → New repository secret**, tạo 2 secret:
   - `TELEGRAM_BOT_TOKEN`: token bot Telegram (lấy từ @BotFather, dùng lại token bot cũ cũng được)
   - `TELEGRAM_CHAT_ID`: ID chat hoặc nhóm nhận tin
4. Vào **Settings → Actions → General → Workflow permissions**, chọn **Read and write permissions** (để bot lưu nhật ký).
5. Chạy thử: vào tab **Actions → Claude Bot TOP5 → Run workflow**.

## 3. Hẹn giờ bằng cron-job.org (giống các bot khác)

Tạo 2 cron job, cùng cấu hình như sau:

- **URL:** `https://api.github.com/repos/karenmanucian-eng/Claude-Bot-Top5/actions/workflows/top5.yml/dispatches`
- **Method:** `POST`
- **Headers:**
  - `Authorization: Bearer <GitHub token có quyền Actions: write>`
  - `Accept: application/vnd.github+json`
- **Body:** `{"ref":"main"}`
- **Time zone:** Asia/Ho_Chi_Minh, **thứ 2 – thứ 6**

| Job | Giờ |
|---|---|
| TOP5 sáng | **11:35** |
| TOP5 chiều | **15:15** |

> Nếu không muốn dùng cron-job.org: mở file `.github/workflows/top5.yml`, bỏ dấu `#` ở 3 dòng `schedule`.
> GitHub có thể chạy trễ 5–30 phút.

## 4. Chạy thử trên máy (E:\CLAUDE_BOT_TOP5_AI)

```bash
pip install -r requirements.txt
python test_offline.py      # chạy bằng dữ liệu giả, kiểm tra logic, không cần mạng
python main.py --dry        # chạy dữ liệu thật, chỉ in ra màn hình, không gửi Telegram
```

## 5. Các file

| File | Việc |
|---|---|
| `main.py` | Luồng chính: tải dữ liệu → lọc → chấm điểm → xếp hạng → gửi |
| `config.py` | **Mọi ngưỡng lọc và trọng số** |
| `vci_client.py` | Lấy giá, danh sách mã, ngành, chỉ số tài chính từ Vietcap |
| `indicators.py` | MA, RSI, MACD, ATR, ADX, đỉnh/đáy |
| `analysis.py` | Chấm điểm kỹ thuật, tài chính, vùng mua/bán/cắt lỗ, thị trường |
| `report.py` | Soạn và gửi tin Telegram |
| `tracker.py` | Ghi nhật ký TOP5 và chấm hiệu quả thật |
| `test_offline.py` | Chạy thử bằng dữ liệu giả |

---

*Bot chỉ để tham khảo, không phải khuyến nghị đầu tư.
Điểm số là bộ luật minh bạch, chưa được backtest; hãy dựa vào phần "Hiệu quả thật" sau vài tuần để đánh giá.*
