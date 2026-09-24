/**
 * ==============================================================================
 * GOOGLE APPS SCRIPT: HE THONG TU DONG THONG KE YEU CAU BAO HIEM PJICO HA GIANG
 * Tai khoan Google quan tri: banhmi2xoi@gmail.com
 * ==============================================================================
 * 
 * HUONG DAN CAI DAT NHANH (DUOI 1 PHUT):
 * ------------------------------------------------------------------------------
 * Buoc 1: Dung tai khoan "banhmi2xoi@gmail.com" truy cap https://sheets.new de tao mot trang tinh Google Sheet moi.
 *         Dat ten trang tinh la: "PJICO HA GIANG - QUAN LY YEU CAU BAO HIEM ONLINE"
 * 
 * Buoc 2: Tren thanh menu, chon: Tien ich mo rong (Extensions) -> Apps Script.
 * 
 * Buoc 3: Xoa toan bo ma mac dinh trong tep Code.gs va DAN TOAN BO MA NGUON NAY VAO.
 * 
 * Buoc 4: Bam nut "Trien khai" (Deploy) o goc tren ben phai -> Chon "Tuy chon trien khai moi" (New deployment).
 *         - Loai trien khai (Select type): Bam icon Banh rang -> Chon "Ung dung web" (Web app).
 *         - Mo ta (Description): "Webhook Nhan Don PJICO".
 *         - Thuc thi duoi dang (Execute as): "Toi" (banhmi2xoi@gmail.com).
 *         - Ai co quyen truy cap (Who has access): "Bat ky ai" (Anyone).
 *         - Nhan nut "Trien khai" (Deploy) -> Cap quyen truy cap neu Google hoi.
 * 
 * Buoc 5: Sao chep duong dan "URL ung dung web" (Web App URL) nhan duoc.
 *         Dan duong link do vao bien GOOGLE_SHEET_WEBHOOK_URL trong file index.html!
 * ==============================================================================
 */

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.tryLock(15000); // Khoa tranh xung dot khi nhieu nguoi gui cung luc

  try {
    var rawData = e.postData ? e.postData.contents : "{}";
    var data = JSON.parse(rawData);

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheetName = "Danh Sach Yeu Cau BH";
    var sheet = ss.getSheetByName(sheetName);

    // Neu trang tinh chua co, tao moi va thiet lap tieu de cot
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
      var headers = [
        "Thời Gian Gửi",
        "Mã Đơn Yêu Cầu",
        "Kênh Tiếp Nhận",
        "Đại Lý Phụ Trách",
        "Họ Tên Bên Mua",
        "Số Điện Thoại",
        "Nhu Cầu Tư Vấn / Ghi Chú",
        "Email",
        "CCCD / MST",
        "Ngày Sinh",
        "Giới Tính",
        "Quốc Tịch",
        "Địa Chỉ Bên Mua",
        "Nghề Nghiệp / Lĩnh Vực",
        "Người ĐBH Khác Bên Mua?",
        "Họ Tên Người ĐBH",
        "CCCD Người ĐBH",
        "Ngày Sinh Người ĐBH",
        "Mối Quan Hệ",
        "Sản Phẩm Bảo Hiểm",
        "Số Tiền BH Mong Muốn",
        "Thời Hạn Bảo Hiểm",
        "Phương Thức Đóng Phí",
        "Khai Báo Sức Khỏe (Cao/Nặng/Nhóm máu)",
        "Tiền Sử Bệnh Lý",
        "Phẫu Thuật / Điều Trị",
        "Thói Quen / Rủi Ro Nghề Nghiệp",
        "Thông Tin Xe / Tài Sản / Hàng Hóa",
        "Người Thụ Hưởng và Tỷ Lệ %",
        "Cam Kết Pháp Lý",
        "Ngày Ký",
        "Có Chữ Ký Điện Tử?"
      ];
      sheet.appendRow(headers);

      // Dinh dang tieu de dep mat (Mau xanh PJICO Petrolimex)
      var headerRange = sheet.getRange(1, 1, 1, headers.length);
      headerRange.setBackground("#002D62");
      headerRange.setFontColor("#FFFFFF");
      headerRange.setFontWeight("bold");
      headerRange.setFontSize(11);
      headerRange.setHorizontalAlignment("center");
      headerRange.setWrap(true);
      sheet.setRowHeight(1, 38);
      sheet.setFrozenRows(1);
    }

    var nowStr = Utilities.formatDate(new Date(), "Asia/Ho_Chi_Minh", "dd/MM/yyyy HH:mm:ss");
    var requestId = data.requestId || ("PJ-" + Utilities.formatDate(new Date(), "Asia/Ho_Chi_Minh", "yyMMdd-HHmmss"));

    var row = [
      nowStr,
      requestId,
      data.contactChannel || "Zalo",
      data.assignedAgent || "Người cấp đơn chính",
      data.buyerName || "",
      data.buyerPhone || "",
      data.buyerNote || "Tôi muốn tư vấn",
      data.buyerEmail || "",
      data.buyerIdNumber || "",
      data.buyerBirthday || "",
      data.buyerGender || "",
      data.buyerNationality || "Việt Nam",
      data.buyerAddress || "",
      data.buyerOccupation || "",
      data.isInsuredDifferent ? "Có (Khác bên mua)" : "Không (Chính là bên mua)",
      data.insuredName || "",
      data.insuredIdNumber || "",
      data.insuredBirthday || "",
      data.insuredRelationship || "",
      data.productType || "",
      data.coverageAmount || "",
      data.insurancePeriod || "",
      data.paymentMethod || "",
      data.healthStats || "",
      data.medicalHistory || "Không",
      data.surgeryAndTreatment || "Không",
      data.lifestyleRisks || "Bình thường",
      data.extraAssetDetails || "",
      data.beneficiaryInfo || "Không chỉ định",
      data.commitmentsAccepted ? "Đã xác nhận đầy đủ 4 điều khoản" : "Chưa đủ",
      data.signedDate || nowStr,
      data.hasSignature ? "Đã ký điện tử" : "Chưa có"
    ];

    sheet.appendRow(row);

    // Tu dong gui email thong bao toi banhmi2xoi@gmail.com
    try {
      var recipient = "banhmi2xoi@gmail.com";
      var subject = "🔔 [ĐƠN YÊU CẦU MỚI] " + (data.productType || "Bảo hiểm PJICO") + " - " + (data.buyerName || "Khách hàng");
      var body = "Kính gửi Bộ phận Cấp đơn PJICO Hà Giang,\n\n" +
                 "Hệ thống vừa ghi nhận một Giấy yêu cầu bảo hiểm trực tuyến mới:\n" +
                 "--------------------------------------------------\n" +
                 "• Mã đơn: " + requestId + "\n" +
                 "• Thời gian gửi: " + nowStr + "\n" +
                 "• Khách hàng: " + (data.buyerName || "") + "\n" +
                 "• Số điện thoại: " + (data.buyerPhone || "") + "\n" +
                 "• Nhu cầu tư vấn: " + (data.buyerNote || "Tôi muốn tư vấn") + "\n" +
                 "• Email: " + (data.buyerEmail || "Chưa cung cấp") + "\n" +
                 "• Sản phẩm yêu cầu: " + (data.productType || "") + "\n" +
                 "• Số tiền BH mong muốn: " + (data.coverageAmount || "Theo quy tắc") + "\n" +
                 "• Kênh tiếp nhận: " + (data.contactChannel || "Zalo") + " (Đại lý: " + (data.assignedAgent || "") + ")\n" +
                 "--------------------------------------------------\n\n" +
                 "Vui lòng mở Google Sheet quản lý hoặc liên hệ khách hàng để kiểm tra chi tiết và cấp đơn bảo hiểm.\n\n" +
                 "Trân trọng,\nPJICO Hà Giang Online System";
      MailApp.sendEmail(recipient, subject, body);
    } catch (mailErr) {
      Logger.log("Email notification error: " + mailErr.toString());
    }

    return ContentService
      .createTextOutput(JSON.stringify({
        status: "success",
        requestId: requestId,
        message: "Hồ sơ yêu cầu bảo hiểm đã được lưu tự động vào Google Sheet!"
      }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (error) {
    return ContentService
      .createTextOutput(JSON.stringify({
        status: "error",
        message: error.toString()
      }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  return ContentService
    .createTextOutput(JSON.stringify({
      status: "online",
      targetAccount: "banhmi2xoi@gmail.com",
      service: "PJICO Ha Giang Insurance Online Webhook API",
      time: new Date().toISOString()
    }))
    .setMimeType(ContentService.MimeType.JSON);
}
