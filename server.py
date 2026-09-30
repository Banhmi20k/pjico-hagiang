"""
PJICO HÀ GIANG - ADMIN BACKEND SERVER & API (CÓ BẢO MẬT ĐĂNG NHẬP)
Phục vụ Landing Page tại '/' và Admin Panel tại '/admin'
Quản lý trực tiếp cơ sở dữ liệu SQLite 'brain.db'
Tài khoản mặc định: admin | Mật khẩu mặc định: pjico@2026
Kiến trúc hệ thống: CUSTOMER -> SURVEY -> APPLICATION -> QUOTE -> ORDER -> PAYMENT -> POLICY
"""

import http.server
import socketserver
import sqlite3
import json
import os
import sys
import hashlib
import secrets
import urllib.parse
import re
from datetime import datetime, timedelta

# Đảm bảo in tiếng Việt trên Windows không bị lỗi cp1252
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

PORT = int(os.environ.get("PORT", 8080))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Quản lý phiên đăng nhập
ACTIVE_SESSIONS = {}  # token -> username

# Đường dẫn file database brain.db
DB_PATHS = [
    os.path.join(BASE_DIR, "brain.db"),
    os.path.abspath(os.path.join(BASE_DIR, "..", "..", "Bảo hiểm PJICO online", "brain.db")),
    os.path.abspath(os.path.join(BASE_DIR, "..", "..", "my-brain", "brain.db")),
]

def get_db_path():
    for p in DB_PATHS:
        if os.path.exists(p):
            return p
    return os.path.join(BASE_DIR, "brain.db")

def get_db_connection():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    """Mã hóa mật khẩu bằng SHA-256 kèm muối bảo mật"""
    return hashlib.sha256((password + "_pjico_hagiang_salt_2026").encode('utf-8')).hexdigest()

def sync_db_copies():
    """Đồng bộ brain.db sang các thư mục liên quan nếu có"""
    src = get_db_path()
    if not os.path.exists(src):
        return
    for p in DB_PATHS:
        if p != src and os.path.exists(os.path.dirname(p)):
            try:
                import shutil
                shutil.copy2(src, p)
            except Exception:
                pass

# =========================================================================
# HELPER FUNCTIONS: VALIDATION, AUDIT LOG, CENTRAL CUSTOMER
# =========================================================================

def normalize_vietnam_phone(phone_str):
    """Chuẩn hóa số điện thoại Việt Nam về dạng 10 chữ số (bắt đầu bằng 0)"""
    if not phone_str:
        return None
    cleaned = re.sub(r'[\s\.\-\(\)]', '', str(phone_str).strip())
    if cleaned.startswith('+84'):
        cleaned = '0' + cleaned[3:]
    elif cleaned.startswith('84') and len(cleaned) == 11:
        cleaned = '0' + cleaned[2:]
    return cleaned

def is_valid_vietnam_phone(phone_str):
    """Kiểm tra số điện thoại Việt Nam hợp lệ (10 chữ số bắt đầu bằng 03, 05, 07, 08, 09)"""
    normalized = normalize_vietnam_phone(phone_str)
    if not normalized:
        return False
    return bool(re.match(r'^0[35789]\d{8}$', normalized))

def log_audit(conn, entity_type, entity_id, action, old_value=None, new_value=None, actor='system', reason=None):
    """Ghi vết kiểm toán (Audit Log) theo dõi các thay đổi quan trọng kèm lý do (Phase 4)"""
    try:
        cur = conn.cursor()
        old_str = json.dumps(old_value, ensure_ascii=False) if isinstance(old_value, (dict, list)) else (str(old_value) if old_value is not None else None)
        new_str = json.dumps(new_value, ensure_ascii=False) if isinstance(new_value, (dict, list)) else (str(new_value) if new_value is not None else None)
        cur.execute("""
            INSERT INTO audit_logs (entity_type, entity_id, action, old_value, new_value, actor, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
        """, (str(entity_type), str(entity_id), str(action), old_str, new_str, str(actor), str(reason) if reason else None))
    except Exception as e:
        print(f"Error logging audit: {e}")

# =========================================================================
# PHASE 4: NOTIFICATION SERVICE, ORDER STATUS RULES & RBAC
# =========================================================================

class NotificationService:
    """Hệ thống trừu tượng hóa thông báo khách hàng (Zalo, SMS, Email, System) (Section 10 & 11)"""
    @staticmethod
    def send(conn, event_type, recipient, data, channel='SYSTEM'):
        order_id = data.get('order_id')
        order_code = data.get('order_code') or str(order_id or '')
        amount = float(data.get('amount') or data.get('total_amount') or 0)
        received = float(data.get('received') or data.get('received_amount') or 0)
        expected = float(data.get('expected') or data.get('expected_amount') or amount)
        tracking_url = f"https://pjicohagiang.xyz/tra-cuu-don-hang?order={order_code}&phone={recipient}"

        msg_map = {
            'ORDER_CREATED': f"PJICO Hà Giang đã ghi nhận yêu cầu đặt mua của Quý khách. Mã đơn: {order_code}. Số tiền: {amount:,.0f} VNĐ. Trạng thái: Chờ thanh toán.",
            'PAYMENT_SUCCESS': f"PJICO Hà Giang xác nhận đã nhận thanh toán đơn {order_code}. Hồ sơ đang được xử lý.",
            'PAYMENT_MISMATCH': f"PJICO Hà Giang thông báo: Giao dịch thanh toán đơn {order_code} chưa đủ số tiền (Đã nhận {received:,.0f}/{expected:,.0f} VNĐ). Quý khách vui lòng liên hệ hotline 0987.501.199 để được hỗ trợ.",
            'ORDER_PROCESSING': f"PJICO Hà Giang đang tiến hành thẩm định và cấp đơn bảo hiểm cho mã đơn {order_code}.",
            'POLICY_ISSUED': f"Giấy chứng nhận/hợp đồng của Quý khách đã được cấp. Mã đơn: {order_code}. Vui lòng truy cập đường dẫn để xem hồ sơ: {tracking_url}",
            'POLICY_DELIVERED': f"Giấy chứng nhận bảo hiểm cho mã đơn {order_code} đã được gửi tới Quý khách thành công. Cảm ơn Quý khách đã đồng hành cùng PJICO Hà Giang."
        }
        msg = msg_map.get(event_type, f"Thông báo PJICO Hà Giang về đơn hàng #{order_code}: {event_type}")
        try:
            cur = conn.cursor()
            db_oid = data.get('db_order_id') or (int(order_id) if str(order_id).isdigit() else None)
            cur.execute("""
                INSERT INTO notifications (order_id, customer_id, recipient, channel, event_type, message, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'SENT', CURRENT_TIMESTAMP);
            """, (db_oid, data.get('customer_id'), str(recipient or '0987501199'), channel, event_type, msg))
            print(f"[NOTIFICATION] [{channel}] To {recipient} ({event_type}): {msg}")
        except Exception as e:
            print(f"Error in NotificationService.send: {e}")
        return msg


class OrderStatusRules:
    """State Machine quản lý luồng chuyển trạng thái đơn hàng nghiêm ngặt (Section 14 & 15)"""
    VALID_FLOW = [
        'NEW',
        'CONSULTING',
        'QUOTED',
        'WAITING_CONFIRM',
        'PENDING_PAYMENT',
        'PAID',
        'PROCESSING',
        'ISSUED',
        'DELIVERED'
    ]

    @classmethod
    def normalize_status(cls, status):
        if not status:
            return 'NEW'
        s = str(status).strip()
        legacy_map = {
            'pending': 'PENDING_PAYMENT',
            'paid': 'PAID',
            'processing': 'PROCESSING',
            'completed': 'ISSUED',
            'cancelled': 'CANCELLED'
        }
        return legacy_map.get(s.lower(), s.upper())

    @classmethod
    def can_transition(cls, current_status, next_status, is_admin=False, is_paid=False):
        cur = cls.normalize_status(current_status)
        nxt = cls.normalize_status(next_status)

        if cur == nxt:
            return True, "Trạng thái không đổi"

        # Cancellation rules (Section 15)
        if nxt == 'CANCELLED':
            if cur in ['NEW', 'CONSULTING', 'QUOTED', 'WAITING_CONFIRM', 'PENDING_PAYMENT']:
                return True, "Hủy đơn hàng hợp lệ"
            elif cur in ['PAID', 'PROCESSING', 'ISSUED', 'DELIVERED']:
                return False, "Đơn hàng đã thanh toán không thể hủy trực tiếp sang CANCELLED. Vui lòng chuyển sang REFUND_PENDING để xử lý hoàn tiền."
            return False, "Không thể hủy đơn hàng từ trạng thái này"

        # Refund flow (Section 15)
        if nxt == 'REFUND_PENDING':
            if cur in ['PAID', 'PROCESSING']:
                return True, "Chuyển sang chờ hoàn tiền"
            return False, "Chỉ đơn hàng đã thanh toán mới có thể yêu cầu hoàn tiền"

        if nxt == 'REFUNDED':
            if cur == 'REFUND_PENDING' or is_admin:
                return True, "Xác nhận đã hoàn tiền"
            return False, "Phải qua bước REFUND_PENDING trước khi xác nhận REFUNDED"

        # Normal forward progression (Section 14)
        if cur in cls.VALID_FLOW and nxt in cls.VALID_FLOW:
            cur_idx = cls.VALID_FLOW.index(cur)
            nxt_idx = cls.VALID_FLOW.index(nxt)

            # Rule: Không cho PENDING_PAYMENT -> ISSUED nếu chưa PAID
            if nxt in ['ISSUED', 'DELIVERED'] and not is_paid and cur_idx < cls.VALID_FLOW.index('PAID'):
                return False, f"Không thể chuyển sang {nxt} khi đơn hàng chưa được thanh toán (PAID)!"

            # 1-step forward
            if nxt_idx == cur_idx + 1:
                return True, "Chuyển bước hợp lệ"
            elif nxt_idx > cur_idx:
                if is_admin:
                    return True, "Quản trị viên duyệt chuyển bước vượt cấp"
                return False, f"Không thể nhảy cóc từ {cur} sang {nxt}. Cần thực hiện tuần tự các bước."
            elif nxt_idx < cur_idx:
                if is_admin:
                    return True, "Quản trị viên quay lại trạng thái trước"
                return False, f"Không được phép quay lùi trạng thái từ {cur} về {nxt}."

        return False, f"Chuyển trạng thái từ {cur} sang {nxt} không được hỗ trợ"


def sanitize_customer_for_public(c):
    """Che giấu thông tin nhạy cảm (CCCD/MST) khi trả dữ liệu qua API public"""
    if not c:
        return None
    d = dict(c)
    if d.get('identity_no'):
        id_str = str(d['identity_no'])
        d['identity_no'] = id_str[:3] + '***' + id_str[-3:] if len(id_str) > 6 else '***'
    else:
        d['identity_no'] = None
    if 'tax_code' in d and d['tax_code']:
        tc = str(d['tax_code'])
        d['tax_code'] = tc[:3] + '***' if len(tc) > 4 else '***'
    d['customer_id'] = d.get('id')
    return d

def find_or_create_customer(conn, phone, full_name, email=None, address=None, identity_no=None, tax_code=None, zalo=None, actor='system'):
    """
    Quy tắc quản lý khách hàng trung tâm (Customer Master Profile):
    - phone là trường nhận diện cốt lõi.
    - Nếu phone đã tồn tại: không tạo duplicate, dùng customer_id hiện tại và cập nhật thông tin nếu có thay đổi.
    - Nếu phone chưa tồn tại: tạo mới customer.
    - Ghi nhận Audit Log mọi hành động tạo mới hoặc cập nhật.
    """
    cur = conn.cursor()
    norm_phone = normalize_vietnam_phone(phone)
    if not norm_phone or not is_valid_vietnam_phone(norm_phone):
        raise ValueError("Số điện thoại không hợp lệ (cần 10 số theo chuẩn mạng di động Việt Nam)")

    cur.execute("SELECT * FROM customers WHERE phone = ?;", (norm_phone,))
    existing = cur.fetchone()
    if existing:
        cust = dict(existing)
        cust_id = cust['id']
        updates = []
        params = []
        old_val = {}
        new_val = {}

        if full_name and full_name.strip() and full_name.strip() != (cust.get('full_name') or cust.get('name')):
            old_val['full_name'] = cust.get('full_name') or cust.get('name')
            new_val['full_name'] = full_name.strip()
            updates.extend(["full_name = ?", "name = ?"])
            params.extend([full_name.strip(), full_name.strip()])

        if email and email.strip() and email.strip() != (cust.get('email') or ''):
            old_val['email'] = cust.get('email')
            new_val['email'] = email.strip()
            updates.append("email = ?")
            params.append(email.strip())

        if address and address.strip() and address.strip() != (cust.get('address') or ''):
            old_val['address'] = cust.get('address')
            new_val['address'] = address.strip()
            updates.append("address = ?")
            params.append(address.strip())

        if identity_no and identity_no.strip() and identity_no.strip() != (cust.get('identity_no') or ''):
            old_val['identity_no'] = cust.get('identity_no')
            new_val['identity_no'] = identity_no.strip()
            updates.append("identity_no = ?")
            params.append(identity_no.strip())

        if tax_code and tax_code.strip() and tax_code.strip() != (cust.get('tax_code') or ''):
            old_val['tax_code'] = cust.get('tax_code')
            new_val['tax_code'] = tax_code.strip()
            updates.append("tax_code = ?")
            params.append(tax_code.strip())

        if zalo and zalo.strip() and zalo.strip() != (cust.get('zalo') or ''):
            updates.append("zalo = ?")
            params.append(zalo.strip())

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE customers SET {', '.join(updates)} WHERE id = ?;"
            params.append(cust_id)
            cur.execute(sql, tuple(params))
            log_audit(conn, 'customer', cust_id, 'UPDATE', old_val, new_val, actor)

        cur.execute("SELECT * FROM customers WHERE id = ?;", (cust_id,))
        res = dict(cur.fetchone())
        res['customer_id'] = res['id']
        return res, False
    else:
        display_name = full_name.strip() if full_name else f"Khách hàng {norm_phone}"
        cur.execute("""
            INSERT INTO customers (name, full_name, phone, email, address, identity_no, tax_code, zalo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """, (display_name, display_name, norm_phone, email, address, identity_no, tax_code, zalo or norm_phone))
        cust_id = cur.lastrowid
        new_record = {
            'id': cust_id,
            'customer_id': cust_id,
            'name': display_name,
            'full_name': display_name,
            'phone': norm_phone,
            'email': email,
            'address': address,
            'identity_no': identity_no,
            'tax_code': tax_code,
            'zalo': zalo or norm_phone
        }
        log_audit(conn, 'customer', cust_id, 'CREATE', None, new_record, actor)
        return new_record, True

# =========================================================================
# KHỞI TẠO VÀ DI TRÚ CƠ SỞ DỮ LIỆU
# =========================================================================

def init_database():
    """Khởi tạo các bảng và tài khoản quản trị mặc định (Có chuẩn hóa toàn bộ Phase 1)"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Bảng tài khoản quản trị (admin_users)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Bảng sản phẩm
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        product_type TEXT NOT NULL CHECK (product_type IN ('physical', 'digital', 'service')),
        price NUMERIC NOT NULL CHECK (price >= 0),
        description TEXT,
        stock_quantity INTEGER CHECK (stock_quantity IS NULL OR stock_quantity >= 0),
        CHECK (
            (product_type = 'physical' AND stock_quantity IS NOT NULL)
            OR product_type IN ('digital', 'service')
        )
    );
    """)

    # 3. Bảng khách hàng (customers)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE,
        zalo TEXT,
        registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Kiểm tra và thêm các cột mở rộng cho customers nếu chưa có
    cur.execute("PRAGMA table_info(customers);")
    cust_cols = [row[1] for row in cur.fetchall()]
    new_cust_cols = [
        ('full_name', 'TEXT'),
        ('email', 'TEXT'),
        ('address', 'TEXT'),
        ('identity_no', 'TEXT'),
        ('tax_code', 'TEXT'),
        ('created_at', 'TEXT'),
        ('updated_at', 'TEXT')
    ]
    for col_name, col_type in new_cust_cols:
        if col_name not in cust_cols:
            cur.execute(f"ALTER TABLE customers ADD COLUMN {col_name} {col_type};")

    cur.execute("UPDATE customers SET full_name = name WHERE full_name IS NULL OR full_name = '';")
    cur.execute("UPDATE customers SET created_at = registered_at WHERE created_at IS NULL;")

    # 4. Bảng khảo sát (surveys)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS surveys (
        survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        full_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        product_id INTEGER,
        product_name TEXT,
        vehicle_type TEXT,
        plate_number TEXT,
        purchase_intent TEXT,
        expected_purchase_time TEXT,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
    );
    """)

    # 5. Bảng giấy yêu cầu bảo hiểm (applications)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        application_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        full_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        address TEXT,
        identity_no TEXT,
        product_id INTEGER,
        insured_object TEXT,
        vehicle_type TEXT,
        plate_number TEXT,
        vehicle_brand TEXT,
        vehicle_model TEXT,
        manufacture_year INTEGER,
        chassis_number TEXT,
        engine_number TEXT,
        beneficiary TEXT,
        other_required_data TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
    );
    """)

    # 6. Bảng báo giá (quotes)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS quotes (
        quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        coverage_summary TEXT,
        premium NUMERIC NOT NULL DEFAULT 0,
        discount NUMERIC NOT NULL DEFAULT 0,
        total_amount NUMERIC NOT NULL DEFAULT 0,
        valid_until TEXT,
        consultant_id TEXT,
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK (status IN ('DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'EXPIRED')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
    );
    """)

    # 7. Bảng đơn hàng (orders)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        amount NUMERIC NOT NULL CHECK (amount >= 0),
        status TEXT NOT NULL DEFAULT 'pending',
        purchased_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT,
        FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
    );
    """)

    # Kiểm tra và thêm các cột mở rộng cho orders nếu chưa có
    cur.execute("PRAGMA table_info(orders);")
    order_cols = [row[1] for row in cur.fetchall()]
    new_order_cols = [
        ('order_code', 'TEXT'),
        ('quote_id', 'INTEGER'),
        ('product_name', 'TEXT'),
        ('customer_name', 'TEXT'),
        ('customer_phone', 'TEXT'),
        ('customer_address', 'TEXT'),
        ('insured_object', 'TEXT'),
        ('vehicle_type', 'TEXT'),
        ('plate_number', 'TEXT'),
        ('insurance_start', 'TEXT'),
        ('insurance_end', 'TEXT'),
        ('premium', 'NUMERIC'),
        ('discount', 'NUMERIC DEFAULT 0'),
        ('total_amount', 'NUMERIC'),
        ('payment_deadline', 'TEXT'),
        ('created_at', 'TEXT'),
        ('updated_at', 'TEXT')
    ]
    for col_name, col_type in new_order_cols:
        if col_name not in order_cols:
            cur.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")

    cur.execute("UPDATE orders SET total_amount = amount WHERE total_amount IS NULL;")
    cur.execute("UPDATE orders SET created_at = purchased_at WHERE created_at IS NULL;")

    # 8. Bảng thanh toán (payments) - Chuẩn hóa Phase 3: Hỗ trợ MANUAL_REVIEW, raw_content và nullable order_id
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        expected_amount NUMERIC NOT NULL DEFAULT 0,
        received_amount NUMERIC DEFAULT 0,
        transaction_id TEXT,
        transaction_code TEXT,
        transaction_time TEXT,
        bank_name TEXT DEFAULT 'VietinBank',
        account_number TEXT DEFAULT '106006104248',
        qr_content TEXT,
        raw_content TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED', 'MISMATCH', 'REFUNDED', 'MANUAL_REVIEW')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
    );
    """)

    # Tự động di trú bảng payments nếu chưa có trạng thái MANUAL_REVIEW
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='payments';")
    pm_sql_row = cur.fetchone()
    if pm_sql_row and 'MANUAL_REVIEW' not in pm_sql_row[0]:
        cur.execute("""
        CREATE TABLE payments_new (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            expected_amount NUMERIC NOT NULL DEFAULT 0,
            received_amount NUMERIC DEFAULT 0,
            transaction_id TEXT,
            transaction_code TEXT,
            transaction_time TEXT,
            bank_name TEXT DEFAULT 'VietinBank',
            account_number TEXT DEFAULT '106006104248',
            qr_content TEXT,
            raw_content TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED', 'MISMATCH', 'REFUNDED', 'MANUAL_REVIEW')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
        );
        """)
        cur.execute("""
        INSERT INTO payments_new (
            payment_id, order_id, expected_amount, received_amount, transaction_id,
            transaction_code, transaction_time, bank_name, account_number, qr_content,
            status, created_at, updated_at
        ) SELECT
            payment_id, order_id, expected_amount, received_amount, transaction_id,
            transaction_code, transaction_time, bank_name, account_number, qr_content,
            status, created_at, updated_at
        FROM payments;
        """)
        cur.execute("DROP TABLE payments;")
        cur.execute("ALTER TABLE payments_new RENAME TO payments;")

    # Phase 4 Migration: Ensure staff roles, policies, notifications, and indexes
    cur.execute("PRAGMA table_info(admin_users);")
    admin_cols = [r[1] for r in cur.fetchall()]
    if 'role' not in admin_cols:
        cur.execute("ALTER TABLE admin_users ADD COLUMN role TEXT DEFAULT 'ADMIN';")

    cur.execute("PRAGMA table_info(orders);")
    order_cols = [r[1] for r in cur.fetchall()]
    for col_name, col_type in [
        ('staff_assigned', 'TEXT'),
        ('policy_number', 'TEXT'),
        ('policy_expiry_date', 'TEXT'),
        ('document_url', 'TEXT')
    ]:
        if col_name not in order_cols:
            cur.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")

    # 10. Bảng Giấy chứng nhận bảo hiểm / Hợp đồng (policies) (Section 13 & 16)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS policies (
        policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_number TEXT UNIQUE NOT NULL,
        order_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        insured_object TEXT,
        plate_number TEXT,
        issue_date TEXT NOT NULL,
        start_date TEXT NOT NULL,
        expiry_date TEXT NOT NULL,
        premium NUMERIC NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'EXPIRED', 'CANCELLED', 'RENEWED')),
        document_url TEXT,
        certificate_data TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE RESTRICT,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
    );
    """)

    # 11. Bảng Thông báo khách hàng (notifications) (Section 10)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        customer_id INTEGER,
        recipient TEXT NOT NULL,
        channel TEXT NOT NULL CHECK (channel IN ('ZALO', 'SMS', 'EMAIL', 'SYSTEM')),
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'SENT' CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 12. Bổ sung trường reason vào audit_logs nếu thiếu
    cur.execute("PRAGMA table_info(audit_logs);")
    audit_cols = [r[1] for r in cur.fetchall()]
    if 'reason' not in audit_cols:
        cur.execute("ALTER TABLE audit_logs ADD COLUMN reason TEXT;")

    # 13. Hiệu năng & Indexes (Section 21)
    indexes = [
        ("idx_customers_phone", "customers", "phone"),
        ("idx_orders_order_code", "orders", "order_code"),
        ("idx_orders_status", "orders", "status"),
        ("idx_orders_created_at", "orders", "created_at"),
        ("idx_orders_plate_number", "orders", "plate_number"),
        ("idx_payments_transaction_id", "payments", "transaction_id"),
        ("idx_payments_status", "payments", "status"),
        ("idx_policies_policy_number", "policies", "policy_number")
    ]
    for idx_name, tbl, col in indexes:
        try:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl}({col});")
        except Exception:
            pass

    # Tạo các tài khoản phân quyền mẫu (Section 17)
    sample_roles = [
        ("admin", "pjico@2026", "ADMIN"),
        ("sales_hagiang", "pjico@sales2026", "SALES"),
        ("ketoan_hagiang", "pjico@ketoan2026", "ACCOUNTING"),
        ("xuly_hagiang", "pjico@xuly2026", "PROCESSING")
    ]
    for u, p, r in sample_roles:
        cur.execute("SELECT id FROM admin_users WHERE username = ?;", (u,))
        if not cur.fetchone():
            cur.execute("INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?);",
                        (u, hash_password(p), r))


    # 9. Bảng kiểm toán thay đổi (audit_logs)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        actor TEXT DEFAULT 'system',
        timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()

    # Tạo tài khoản admin mặc định: admin / pjico@2026 nếu chưa có
    admin_count = cur.execute("SELECT COUNT(*) FROM admin_users;").fetchone()[0]
    if admin_count == 0:
        default_user = "admin"
        default_pass = "pjico@2026"
        cur.execute(
            "INSERT INTO admin_users (username, password_hash) VALUES (?, ?);",
            (default_user, hash_password(default_pass))
        )
        conn.commit()

    # Dữ liệu mẫu sản phẩm nếu rỗng
    prod_count = cur.execute("SELECT COUNT(*) FROM products;").fetchone()[0]
    if prod_count == 0:
        sample_prods = [
            ("Ấn chỉ & Thẻ cứng BH Xe Máy (Giao tận nhà)", "physical", 86000, "Thẻ bảo hiểm cứng ép plastic kèm tem chuẩn PJICO", 50),
            ("Mũ bảo hiểm PJICO Petrolimex (Quà tặng)", "physical", 150000, "Mũ bảo hiểm đạt chuẩn CR in logo PJICO Petrolimex", 30),
            ("Bảo hiểm Xe Máy TNDS Điện Tử (Tích Xanh)", "digital", 86000, "Giấy chứng nhận điện tử nhận ngay qua Zalo/Email trong 3 phút", None),
            ("Bảo hiểm Ô Tô TNDS 5 Chỗ Điện Tử", "digital", 480700, "GCN điện tử hợp chuẩn xét đăng kiểm toàn quốc", None),
            ("Gói Cứu Hộ Giao Thông Khẩn Cấp 24/7", "service", 300000, "Cứu hộ kéo xe, kích bình ắc quy trên toàn địa bàn Hà Giang", None),
            ("Dịch Vụ Tư Vấn Thẩm Định Hồ Sơ Visa Châu Âu", "service", 500000, "Tư vấn hồ sơ bảo hiểm du lịch chuẩn quy định đại sứ quán", None)
        ]
        cur.executemany("INSERT INTO products (name, product_type, price, description, stock_quantity) VALUES (?, ?, ?, ?, ?);", sample_prods)
        conn.commit()

    # Dữ liệu mẫu khách hàng nếu rỗng
    cust_count = cur.execute("SELECT COUNT(*) FROM customers;").fetchone()[0]
    if cust_count == 0:
        sample_custs = [
            ("Nguyễn Văn Bình", "Nguyễn Văn Bình", "0987501199", "binh@gmail.com", "TP. Hà Giang", "0987501199"),
            ("Trần Thị Mai", "Trần Thị Mai", "0912345678", "mai@gmail.com", "Vị Xuyên, Hà Giang", "0912345678"),
            ("Hoàng Văn Đức", "Hoàng Văn Đức", "0906069368", "duc@gmail.com", "Đồng Văn, Hà Giang", "0906069368")
        ]
        cur.executemany("INSERT INTO customers (name, full_name, phone, email, address, zalo) VALUES (?, ?, ?, ?, ?, ?);", sample_custs)
        conn.commit()

    conn.close()
    sync_db_copies()


# =========================================================================
# HTTP REQUEST HANDLER VỚI ĐẦY ĐỦ CÁC ENDPOINT PHASE 1
# =========================================================================

class AdminRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def send_json(self, data, status_code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status_code=400):
        self.send_json({"success": False, "error": message}, status_code)

    def get_parsed_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            raw_body = self.rfile.read(content_length).decode('utf-8')
            return json.loads(raw_body)
        return {}

    def is_authenticated(self):
        """Kiểm tra token xác thực từ header Authorization: Bearer <token>"""
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ', 1)[1].strip()
            if token in ACTIVE_SESSIONS:
                return True
        return False

    def get_current_user_info(self):
        """Lấy username và role của người dùng hiện tại (Section 17)"""
        username = self.get_current_username()
        if not username:
            return None, None
        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT role FROM admin_users WHERE username = ?;", (username,)).fetchone()
        conn.close()
        role = row['role'].upper() if (row and row['role']) else ('ADMIN' if username == 'admin' else 'SALES')
        return username, role

    def get_current_username(self):
        """Lấy username của quản trị viên từ token hiện tại"""
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ', 1)[1].strip()
            if isinstance(ACTIVE_SESSIONS, dict):
                return ACTIVE_SESSIONS.get(token)
            elif token in ACTIVE_SESSIONS:
                return 'admin'
        return None

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        parts = parsed_url.path.strip('/').split('/')

        # 1. Định tuyến giao diện Web
        if path == '/admin':
            admin_file = os.path.join(BASE_DIR, "admin", "index.html")
            if not os.path.exists(admin_file):
                admin_file = os.path.join(BASE_DIR, "admin.html")
            if os.path.exists(admin_file):
                with open(admin_file, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        if path in ['/thanhtoan', '/tra-cuu-don-hang', '/tra-cuu', '/tracking']:
            thanhtoan_file = os.path.join(BASE_DIR, "thanhtoan", "index.html")
            if not os.path.exists(thanhtoan_file):
                thanhtoan_file = os.path.join(BASE_DIR, "thanhtoan.html")
            if os.path.exists(thanhtoan_file):
                with open(thanhtoan_file, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # 1.5. Phục vụ trang tra cứu đơn hàng công khai /tra-cuu-don-hang (Section 12)
        if path in ['/tra-cuu-don-hang', '/tracuu']:
            tracuu_file = os.path.join(BASE_DIR, "tra-cuu-don-hang.html")
            if not os.path.exists(tracuu_file):
                tracuu_file = os.path.join(BASE_DIR, "thanhtoan.html")
            if os.path.exists(tracuu_file):
                with open(tracuu_file, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # 2. Public API Endpoints (Kiểm tra đơn, tra cứu khách hàng theo SĐT để auto-fill)
        # GET /api/public/orders/track (Tra cứu đơn hàng bắt buộc SĐT + Mã đơn - Section 12)
        if path == '/api/public/orders/track':
            self.handle_public_track_order(parsed_url.query)
            return

        # GET /api/documents/:id/download (Tải / Xem Giấy chứng nhận bảo hiểm an toàn - Section 13)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'documents' and parts[3] == 'download':
            self.handle_download_document(parts[2], parsed_url.query)
            return

        # GET /api/customers/:id/profile (Hồ sơ khách hàng 360 độ - Section 5)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'customers' and parts[3] == 'profile':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_get_customer_profile(parts[2])
            return

        # GET /api/admin/kpis (Báo cáo kinh doanh & tỷ lệ chuyển đổi phễu - Section 19)
        if path == '/api/admin/kpis':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_admin_kpis()
            return

        # GET /api/admin/renewals (Danh sách hợp đồng sắp hết hạn - Section 16)
        if path == '/api/admin/renewals':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_admin_renewals()
            return

        # GET /api/audit-logs (Lịch sử thay đổi hệ thống - Section 18)
        if path == '/api/audit-logs':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_get_audit_logs(parsed_url.query)
            return

        if path == '/api/public/orders/check':
            self.handle_public_check_order(parsed_url.query)
            return

        # GET /api/customers/by-phone/:phone
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'customers' and parts[2] == 'by-phone':
            self.handle_get_customer_by_phone(parts[3])
            return

        # GET /api/orders/by-phone/:phone
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[2] == 'by-phone':
            self.handle_get_orders_by_phone(parts[3])
            return

        # GET /api/orders/:id/payment-status (Chuẩn hóa Phase 3: Tra cứu trạng thái thanh toán)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'payment-status':
            self.handle_get_order_payment_status(parts[2])
            return

        # GET /api/<resource>/:id
        if len(parts) == 3 and parts[0] == 'api':
            resource = parts[1]
            item_id = parts[2]
            if resource == 'customers':
                self.handle_get_customer_by_id(item_id)
                return
            elif resource == 'surveys':
                self.handle_get_survey_by_id(item_id)
                return
            elif resource == 'applications':
                self.handle_get_application_by_id(item_id)
                return
            elif resource == 'quotes':
                self.handle_get_quote_by_id(item_id)
                return
            elif resource == 'orders':
                if self.is_authenticated():
                    self.handle_get_order_detail(item_id)
                else:
                    self.handle_get_order_by_id(item_id)
                return

        # 3. API Endpoints bảo mật (Yêu cầu đăng nhập quản trị viên)
        if path == '/api/admin/payments':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_admin_get_payments(parsed_url.query)
            return

        if path in ('/api/stats', '/api/products', '/api/customers', '/api/orders', '/api/surveys', '/api/applications', '/api/quotes', '/api/verify-token'):
            if not self.is_authenticated():
                self.send_error_json("Phiên làm việc hết hạn hoặc chưa đăng nhập", 401)
                return

            if path == '/api/verify-token':
                self.send_json({"success": True, "message": "Token hợp lệ"})
                return
            elif path == '/api/stats':
                self.handle_get_stats()
                return
            elif path == '/api/products':
                self.handle_get_products()
                return
            elif path == '/api/customers':
                self.handle_get_customers()
                return
            elif path == '/api/orders':
                self.handle_get_orders()
                return
            elif path == '/api/surveys':
                self.handle_get_all_surveys()
                return
            elif path == '/api/applications':
                self.handle_get_all_applications()
                return
            elif path == '/api/quotes':
                self.handle_get_all_quotes()
                return

        # Mặc định phục vụ static files (Landing page, ảnh...)
        super().do_GET()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        parts = parsed_url.path.strip('/').split('/')

        try:
            payload = self.get_parsed_body()
        except Exception as e:
            self.send_error_json("Dữ liệu JSON không hợp lệ: " + str(e))
            return

        # 1. Đăng nhập không cần token trước
        if path == '/api/login':
            self.handle_login(payload)
            return

        # 2. Khảo sát nhanh từ website (POST /api/surveys)
        if path == '/api/surveys':
            self.handle_create_survey(payload)
            return

        # 3. Giấy yêu cầu bảo hiểm (POST /api/applications)
        if path == '/api/applications':
            self.handle_create_application(payload)
            return

        # 4. Báo giá (POST /api/quotes)
        if path == '/api/quotes':
            self.handle_create_quote(payload)
            return

        # 5. Đơn hàng (POST /api/orders)
        if path == '/api/orders':
            self.handle_create_order(payload)
            return

        # 6. Khách hàng (POST /api/customers)
        if path == '/api/customers':
            self.handle_create_customer(payload)
            return

        # 7. Tạo đơn hàng từ cổng thanh toán /thanhtoan (Backward compatible)
        if path == '/api/public/orders':
            self.handle_public_create_order(payload)
            return

        # POST /api/orders/:id/transition (Chuyển trạng thái đơn hàng theo quy tắc & RBAC - Section 14)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'transition':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_order_transition(parts[2], payload)
            return

        # POST /api/orders/:id/issue-policy (Cấp giấy chứng nhận bảo hiểm điện tử - Section 2 & 14)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'issue-policy':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_issue_policy(parts[2], payload)
            return

        # POST /api/orders/:id/deliver-policy (Đánh dấu đã gửi khách & thông báo - Section 2 & 14)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'deliver-policy':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_deliver_policy(parts[2], payload)
            return

        # POST /api/admin/payments/:id/match (Khớp giao dịch Manual Review với đơn hàng - Section 9)
        if len(parts) == 5 and parts[0] == 'api' and parts[1] == 'admin' and parts[2] == 'payments' and parts[4] == 'match':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                return
            self.handle_admin_match_payment(parts[3], payload)
            return

        # POST /api/orders/:id/reopen-payment (Tạo lại/Gia hạn thanh toán khi đơn hết hạn)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'reopen-payment':
            self.handle_reopen_order_payment(parts[2], payload)
            return

        # POST /api/orders/:id/confirm-payment (Admin duyệt thanh toán thủ công)
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'orders' and parts[3] == 'confirm-payment':
            if not self.is_authenticated():
                self.send_error_json("Yêu cầu quyền quản trị viên để duyệt thanh toán", 401)
                return
            self.handle_admin_confirm_payment(parts[2], payload)
            return

        # 8. Xác nhận thanh toán (Audit & Khóa chặt: Chỉ cho phép Quản trị viên đã đăng nhập)
        if path == '/api/public/orders/confirm':
            if not self.is_authenticated():
                self.send_error_json("Quyền truy cập bị từ chối: Không cho phép tự đánh dấu đơn hàng đã thanh toán qua client.", 401)
                return
            self.handle_public_confirm_order(payload)
            return

        # 9. Webhook biến động số dư SePay (VietinBank SEVQR)
        if path == '/api/sepay/webhook':
            self.handle_sepay_webhook(payload)
            return

        # Các API POST khác đều cần xác thực
        if not self.is_authenticated():
            self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
            return

        if path == '/api/change-password':
            self.handle_change_password(payload)
        elif path == '/api/logout':
            self.handle_logout()
        elif path == '/api/products':
            self.handle_create_product(payload)
        else:
            self.send_error_json("Endpoint không tồn tại", 404)

    def do_PUT(self):
        parsed_url = urllib.parse.urlparse(self.path)
        parts = parsed_url.path.strip('/').split('/')

        try:
            payload = self.get_parsed_body()
        except Exception as e:
            self.send_error_json("Dữ liệu JSON không hợp lệ: " + str(e))
            return

        if len(parts) == 3 and parts[0] == 'api':
            resource = parts[1]
            item_id = parts[2]

            # PUT /api/customers/:id
            if resource == 'customers':
                self.handle_update_customer(item_id, payload)
                return
            elif resource == 'quotes':
                self.handle_update_quote(item_id, payload)
                return
            elif resource == 'orders':
                self.handle_update_order(item_id, payload)
                return
            elif resource == 'products':
                if not self.is_authenticated():
                    self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
                    return
                self.handle_update_product(int(item_id), payload)
                return
            else:
                self.send_error_json("Resource không tồn tại", 404)
        else:
            self.send_error_json("Endpoint không tồn tại", 404)

    def do_DELETE(self):
        if not self.is_authenticated():
            self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
            return

        parsed_url = urllib.parse.urlparse(self.path)
        parts = parsed_url.path.strip('/').split('/')

        if len(parts) == 3 and parts[0] == 'api':
            resource = parts[1]
            try:
                item_id = int(parts[2])
            except ValueError:
                self.send_error_json("ID phải là số nguyên", 400)
                return

            if resource == 'products':
                self.handle_delete_product(item_id)
            elif resource == 'customers':
                self.handle_delete_customer(item_id)
            elif resource == 'orders':
                self.handle_delete_order(item_id)
            else:
                self.send_error_json("Resource không tồn tại", 404)
        else:
            self.send_error_json("Endpoint không tồn tại", 404)

    # =========================================================================
    # XỬ LÝ ĐĂNG NHẬP, ĐĂNG XUẤT, ĐỔI MẬT KHẨU
    # =========================================================================
    def handle_login(self, data):
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if not username or not password:
            self.send_error_json("Vui lòng nhập tên đăng nhập và mật khẩu", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        user_row = cur.execute(
            "SELECT * FROM admin_users WHERE username = ?;",
            (username,)
        ).fetchone()
        conn.close()

        if not user_row:
            self.send_error_json("Tên đăng nhập hoặc mật khẩu không chính xác", 401)
            return

        user = dict(user_row)
        if user['password_hash'] != hash_password(password):
            self.send_error_json("Tên đăng nhập hoặc mật khẩu không chính xác", 401)
            return

        token = secrets.token_hex(24)
        ACTIVE_SESSIONS[token] = username

        user_role = user.get('role') or ('ADMIN' if username == 'admin' else 'SALES')
        self.send_json({
            "success": True,
            "message": "Đăng nhập thành công",
            "token": token,
            "username": username,
            "role": user_role,
            "user": {
                "id": user['id'],
                "username": user['username'],
                "role": user_role
            }
        })

    def handle_logout(self):
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ', 1)[1].strip()
            ACTIVE_SESSIONS.pop(token, None)
        self.send_json({"success": True, "message": "Đăng xuất thành công"})

    def handle_change_password(self, data):
        old_pass = data.get('old_password', '').strip()
        new_pass = data.get('new_password', '').strip()

        if not old_pass or not new_pass:
            self.send_error_json("Vui lòng nhập mật khẩu cũ và mới", 400)
            return

        if len(new_pass) < 6:
            self.send_error_json("Mật khẩu mới phải có ít nhất 6 ký tự", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        user_row = cur.execute("SELECT * FROM admin_users LIMIT 1;").fetchone()

        if not user_row or dict(user_row)['password_hash'] != hash_password(old_pass):
            conn.close()
            self.send_error_json("Mật khẩu hiện tại không chính xác", 400)
            return

        user_id = dict(user_row)['id']
        cur.execute(
            "UPDATE admin_users SET password_hash = ? WHERE id = ?;",
            (hash_password(new_pass), user_id)
        )
        conn.commit()
        conn.close()
        sync_db_copies()
        self.send_json({"success": True, "message": "Đổi mật khẩu thành công!"})

    # =========================================================================
    # SẢN PHẨM (PRODUCTS)
    # =========================================================================
    def handle_get_products(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM products ORDER BY id ASC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_create_product(self, data):
        name = data.get('name', '').strip()
        p_type = data.get('product_type', '').strip()
        price = data.get('price')
        desc = data.get('description', '').strip()
        stock = data.get('stock_quantity')

        if not name:
            self.send_error_json("Tên sản phẩm không được để trống")
            return
        if p_type not in ('physical', 'digital', 'service'):
            self.send_error_json("Loại sản phẩm phải là physical, digital hoặc service")
            return
        try:
            price = float(price)
            if price < 0:
                raise ValueError()
        except (ValueError, TypeError):
            self.send_error_json("Giá sản phẩm phải là số không âm")
            return

        if p_type == 'physical':
            try:
                stock = int(stock)
                if stock < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self.send_error_json("Sản phẩm vật lý bắt buộc phải có số lượng tồn kho (>= 0)")
                return
        else:
            stock = None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO products (name, product_type, price, description, stock_quantity) VALUES (?, ?, ?, ?, ?);",
                (name, p_type, price, desc, stock)
            )
            new_id = cur.lastrowid
            log_audit(conn, 'product', new_id, 'CREATE', None, {'name': name, 'price': price, 'type': p_type}, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã thêm sản phẩm thành công", "id": new_id})
        except Exception as e:
            self.send_error_json("Lỗi khi thêm sản phẩm: " + str(e))
        finally:
            conn.close()

    def handle_update_product(self, item_id, data):
        name = data.get('name', '').strip()
        p_type = data.get('product_type', '').strip()
        price = data.get('price')
        desc = data.get('description', '').strip()
        stock = data.get('stock_quantity')

        if not name:
            self.send_error_json("Tên sản phẩm không được để trống")
            return
        if p_type not in ('physical', 'digital', 'service'):
            self.send_error_json("Loại sản phẩm phải là physical, digital hoặc service")
            return
        try:
            price = float(price)
            if price < 0:
                raise ValueError()
        except (ValueError, TypeError):
            self.send_error_json("Giá sản phẩm phải là số không âm")
            return

        if p_type == 'physical':
            try:
                stock = int(stock)
                if stock < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self.send_error_json("Sản phẩm vật lý bắt buộc phải có số lượng tồn kho (>= 0)")
                return
        else:
            stock = None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE products SET name = ?, product_type = ?, price = ?, description = ?, stock_quantity = ? WHERE id = ?;",
                (name, p_type, price, desc, stock, item_id)
            )
            log_audit(conn, 'product', item_id, 'UPDATE', None, {'name': name, 'price': price, 'type': p_type}, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã cập nhật sản phẩm thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi cập nhật sản phẩm: " + str(e))
        finally:
            conn.close()

    def handle_delete_product(self, item_id):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            orders_with_prod = cur.execute("SELECT COUNT(*) FROM orders WHERE product_id = ?;", (item_id,)).fetchone()[0]
            if orders_with_prod > 0:
                self.send_error_json(f"Không thể xóa sản phẩm vì đã có {orders_with_prod} đơn hàng liên kết!")
                return

            cur.execute("DELETE FROM products WHERE id = ?;", (item_id,))
            log_audit(conn, 'product', item_id, 'DELETE', None, None, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa sản phẩm thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa sản phẩm: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # 1. CUSTOMER (HỒ SƠ KHÁCH HÀNG TRUNG TÂM)
    # =========================================================================
    def handle_get_customers(self):
        """Admin lấy toàn bộ danh sách khách hàng"""
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM customers ORDER BY id DESC;").fetchall()
        data = []
        for r in rows:
            d = dict(r)
            d['customer_id'] = d.get('id')
            d['full_name'] = d.get('full_name') or d.get('name')
            data.append(d)
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_get_customer_by_id(self, item_id):
        """Lấy thông tin khách hàng theo ID"""
        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM customers WHERE id = ?;", (item_id,)).fetchone()
        conn.close()
        if not row:
            self.send_error_json("Không tìm thấy khách hàng", 404)
            return

        cust = dict(row)
        cust['customer_id'] = cust.get('id')
        cust['full_name'] = cust.get('full_name') or cust.get('name')
        if not self.is_authenticated():
            cust = sanitize_customer_for_public(cust)
        self.send_json({"success": True, "data": cust})

    def handle_get_customer_by_phone(self, phone):
        """Tra cứu khách hàng theo SĐT (Phục vụ auto-fill dữ liệu, bảo mật CCCD)"""
        norm_phone = normalize_vietnam_phone(phone)
        if not norm_phone:
            self.send_error_json("Số điện thoại không hợp lệ", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM customers WHERE phone = ?;", (norm_phone,)).fetchone()

        if not row:
            conn.close()
            self.send_json({"success": True, "found": False, "data": None})
            return

        cust = dict(row)
        cust_id = cust['id']
        cust['customer_id'] = cust_id
        cust['full_name'] = cust.get('full_name') or cust.get('name')

        # Tìm thêm thông tin xe / địa chỉ gần nhất từ application hoặc survey để auto-fill tiện lợi
        cur.execute("SELECT * FROM applications WHERE customer_id = ? ORDER BY application_id DESC LIMIT 1;", (cust_id,))
        app_row = cur.fetchone()
        if app_row:
            app_dict = dict(app_row)
            cust['latest_vehicle_type'] = app_dict.get('vehicle_type')
            cust['latest_plate_number'] = app_dict.get('plate_number')
            if not cust.get('address'):
                cust['address'] = app_dict.get('address')
        else:
            cur.execute("SELECT * FROM surveys WHERE customer_id = ? ORDER BY survey_id DESC LIMIT 1;", (cust_id,))
            surv_row = cur.fetchone()
            if surv_row:
                surv_dict = dict(surv_row)
                cust['latest_vehicle_type'] = surv_dict.get('vehicle_type')
                cust['latest_plate_number'] = surv_dict.get('plate_number')

        conn.close()

        if not self.is_authenticated():
            cust = sanitize_customer_for_public(cust)

        self.send_json({"success": True, "found": True, "data": cust})

    def handle_create_customer(self, data):
        """Tạo hoặc lấy thông tin khách hàng (Không tạo duplicate theo SĐT)"""
        full_name = str(data.get('full_name') or data.get('name') or '').strip()
        phone = data.get('phone', '').strip()
        email = data.get('email', '').strip() or None
        address = data.get('address', '').strip() or None
        identity_no = data.get('identity_no', '').strip() or None
        tax_code = data.get('tax_code', '').strip() or None
        zalo = data.get('zalo', '').strip() or None

        if not full_name:
            self.send_error_json("Họ và tên khách hàng không được để trống", 400)
            return

        if not is_valid_vietnam_phone(phone):
            self.send_error_json("Số điện thoại không hợp lệ (cần 10 số theo chuẩn Việt Nam)", 400)
            return

        conn = get_db_connection()
        try:
            actor = 'admin' if self.is_authenticated() else 'customer'
            cust, is_new = find_or_create_customer(
                conn, phone, full_name,
                email=email, address=address, identity_no=identity_no,
                tax_code=tax_code, zalo=zalo, actor=actor
            )
            conn.commit()
            sync_db_copies()

            msg = "Đã thêm khách hàng mới thành công" if is_new else "Đã nhận diện khách hàng hiện tại (Không tạo duplicate)"
            self.send_json({
                "success": True,
                "message": msg,
                "is_new": is_new,
                "customer_id": cust['id'],
                "data": sanitize_customer_for_public(cust) if not self.is_authenticated() else cust
            }, 201 if is_new else 200)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi lưu khách hàng: " + str(e))
        finally:
            conn.close()

    def handle_update_customer(self, item_id, data):
        """Cập nhật thông tin khách hàng"""
        full_name = str(data.get('full_name') or data.get('name') or '').strip()
        phone = data.get('phone', '').strip() or None
        email = data.get('email', '').strip() or None
        address = data.get('address', '').strip() or None
        identity_no = data.get('identity_no', '').strip() or None
        tax_code = data.get('tax_code', '').strip() or None
        zalo = data.get('zalo', '').strip() or None

        if not full_name:
            self.send_error_json("Họ và tên khách hàng không được để trống", 400)
            return

        if phone and not is_valid_vietnam_phone(phone):
            self.send_error_json("Số điện thoại không hợp lệ", 400)
            return

        norm_phone = normalize_vietnam_phone(phone) if phone else None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            old_row = cur.execute("SELECT * FROM customers WHERE id = ?;", (item_id,)).fetchone()
            if not old_row:
                self.send_error_json("Không tìm thấy khách hàng", 404)
                return

            old_dict = dict(old_row)
            actor = 'admin' if self.is_authenticated() else 'customer'

            cur.execute("""
                UPDATE customers
                SET name = ?, full_name = ?, phone = COALESCE(?, phone),
                    email = ?, address = ?, identity_no = COALESCE(?, identity_no),
                    tax_code = COALESCE(?, tax_code), zalo = COALESCE(?, zalo),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (full_name, full_name, norm_phone, email, address, identity_no, tax_code, zalo, item_id))

            log_audit(conn, 'customer', item_id, 'UPDATE', old_dict, data, actor)
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã cập nhật thông tin khách hàng thành công"})
        except sqlite3.IntegrityError:
            conn.rollback()
            self.send_error_json("Số điện thoại này đã được sử dụng cho khách hàng khác")
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi cập nhật khách hàng: " + str(e))
        finally:
            conn.close()

    def handle_delete_customer(self, item_id):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            orders_with_cust = cur.execute("SELECT COUNT(*) FROM orders WHERE customer_id = ?;", (item_id,)).fetchone()[0]
            if orders_with_cust > 0:
                self.send_error_json(f"Không thể xóa khách hàng vì đã có {orders_with_cust} đơn hàng liên kết!")
                return

            cur.execute("DELETE FROM customers WHERE id = ?;", (item_id,))
            log_audit(conn, 'customer', item_id, 'DELETE', None, None, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa khách hàng thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa khách hàng: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # 2. SURVEY (KHẢO SÁT NHANH)
    # =========================================================================
    def handle_create_survey(self, data):
        """Khách hàng nộp khảo sát -> Liên kết customer_id (Tạo mới hoặc dùng customer cũ)"""
        full_name = str(data.get('full_name') or data.get('name') or '').strip()
        phone = str(data.get('phone') or '').strip()

        if not full_name:
            self.send_error_json("Vui lòng điền họ và tên", 400)
            return

        if not is_valid_vietnam_phone(phone):
            self.send_error_json("Số điện thoại không hợp lệ", 400)
            return

        product_id = data.get('product_id')
        product_name = data.get('product_name') or "Bảo hiểm Xe cơ giới"
        vehicle_type = data.get('vehicle_type') or data.get('vehicle')
        plate_number = data.get('plate_number')
        purchase_intent = data.get('purchase_intent') or data.get('status')
        expected_purchase_time = data.get('expected_purchase_time')
        note = data.get('note') or data.get('buyerNote')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Tìm hoặc tạo khách hàng trung tâm
            cust, is_new_cust = find_or_create_customer(conn, phone, full_name, actor='customer')
            cust_id = cust['id']

            # 2. Tạo SURVEY mới liên kết customer_id
            cur.execute("""
                INSERT INTO surveys (customer_id, full_name, phone, product_id, product_name, vehicle_type, plate_number, purchase_intent, expected_purchase_time, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """, (cust_id, full_name, normalize_vietnam_phone(phone), product_id, product_name, vehicle_type, plate_number, purchase_intent, expected_purchase_time, note))

            survey_id = cur.lastrowid
            log_audit(conn, 'survey', survey_id, 'CREATE', None, data, actor='customer')
            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Đã lưu kết quả khảo sát thành công",
                "survey_id": survey_id,
                "customer_id": cust_id,
                "customer": sanitize_customer_for_public(cust)
            }, 201)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi lưu khảo sát: " + str(e))
        finally:
            conn.close()

    def handle_get_survey_by_id(self, survey_id):
        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM surveys WHERE survey_id = ?;", (survey_id,)).fetchone()
        conn.close()
        if not row:
            self.send_error_json("Không tìm thấy khảo sát", 404)
            return
        self.send_json({"success": True, "data": dict(row)})

    def handle_get_all_surveys(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT s.*, c.email as customer_email FROM surveys s JOIN customers c ON s.customer_id = c.id ORDER BY s.survey_id DESC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    # =========================================================================
    # 3. APPLICATION (GIẤY YÊU CẦU BẢO HIỂM)
    # =========================================================================
    def handle_create_application(self, data):
        """Khách nộp giấy yêu cầu -> Kết nối và cập nhật CUSTOMER, tạo APPLICATION"""
        full_name = str(data.get('full_name') or data.get('buyerName') or '').strip()
        phone = str(data.get('phone') or data.get('buyerPhone') or '').strip()

        if not full_name:
            self.send_error_json("Họ và tên người yêu cầu không được để trống", 400)
            return

        if not is_valid_vietnam_phone(phone):
            self.send_error_json("Số điện thoại không hợp lệ", 400)
            return

        address = data.get('address') or data.get('buyerAddress')
        identity_no = data.get('identity_no') or data.get('buyerIdNumber')
        product_id = data.get('product_id')
        insured_object = data.get('insured_object') or data.get('insuredName')
        vehicle_type = data.get('vehicle_type')
        plate_number = data.get('plate_number') or data.get('vehiclePlate')
        vehicle_brand = data.get('vehicle_brand') or data.get('vehicleBrandModel')
        vehicle_model = data.get('vehicle_model')
        manufacture_year = data.get('manufacture_year') or data.get('vehicleYear')
        chassis_number = data.get('chassis_number') or data.get('vehicleChassis')
        engine_number = data.get('engine_number')
        beneficiary = data.get('beneficiary') or data.get('beneficiaryName')
        other_data = data.get('other_required_data')
        other_str = json.dumps(other_data, ensure_ascii=False) if isinstance(other_data, (dict, list)) else (str(other_data) if other_data else None)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Tìm hoặc cập nhật khách hàng trung tâm
            cust, _ = find_or_create_customer(
                conn, phone, full_name,
                address=address, identity_no=identity_no,
                email=data.get('email') or data.get('buyerEmail'),
                actor='customer'
            )
            cust_id = cust['id']

            cur.execute("""
                INSERT INTO applications (
                    customer_id, full_name, phone, address, identity_no,
                    product_id, insured_object, vehicle_type, plate_number,
                    vehicle_brand, vehicle_model, manufacture_year,
                    chassis_number, engine_number, beneficiary, other_required_data,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """, (
                cust_id, full_name, normalize_vietnam_phone(phone), address, identity_no,
                product_id, insured_object, vehicle_type, plate_number,
                vehicle_brand, vehicle_model, manufacture_year,
                chassis_number, engine_number, beneficiary, other_str
            ))

            app_id = cur.lastrowid
            log_audit(conn, 'application', app_id, 'CREATE', None, data, actor='customer')
            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Đã lưu giấy yêu cầu bảo hiểm thành công",
                "application_id": app_id,
                "customer_id": cust_id,
                "customer": sanitize_customer_for_public(cust)
            }, 201)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi lưu giấy yêu cầu: " + str(e))
        finally:
            conn.close()

    def handle_get_application_by_id(self, app_id):
        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM applications WHERE application_id = ?;", (app_id,)).fetchone()
        conn.close()
        if not row:
            self.send_error_json("Không tìm thấy giấy yêu cầu", 404)
            return

        d = dict(row)
        if not self.is_authenticated():
            if d.get('identity_no'):
                d['identity_no'] = d['identity_no'][:3] + '***' if len(d['identity_no']) > 4 else '***'
        self.send_json({"success": True, "data": d})

    def handle_get_all_applications(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM applications ORDER BY application_id DESC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    # =========================================================================
    # 4. QUOTE (BÁO GIÁ)
    # =========================================================================
    def handle_create_quote(self, data):
        customer_id = data.get('customer_id')
        phone = data.get('phone')
        full_name = data.get('full_name') or data.get('customer_name')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            if not customer_id and phone:
                cust, _ = find_or_create_customer(conn, phone, full_name or f"Khách {phone}", actor='consultant')
                customer_id = cust['id']
            elif not customer_id:
                self.send_error_json("Thiếu customer_id hoặc phone", 400)
                return

            product_id = data.get('product_id')
            product_name = data.get('product_name') or "Bảo hiểm PJICO"
            coverage_summary = data.get('coverage_summary')
            premium = float(data.get('premium') or 0)
            discount = float(data.get('discount') or 0)
            total_amount = float(data.get('total_amount') or (premium - discount))
            valid_until = data.get('valid_until')
            consultant_id = data.get('consultant_id')
            status = data.get('status', 'DRAFT').upper()
            if status not in ('DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'EXPIRED'):
                status = 'DRAFT'

            cur.execute("""
                INSERT INTO quotes (
                    customer_id, product_id, product_name, coverage_summary,
                    premium, discount, total_amount, valid_until,
                    consultant_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """, (customer_id, product_id, product_name, coverage_summary, premium, discount, total_amount, valid_until, consultant_id, status))

            quote_id = cur.lastrowid
            log_audit(conn, 'quote', quote_id, 'CREATE', None, data, actor='consultant')
            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Đã tạo bảng báo giá thành công",
                "quote_id": quote_id,
                "customer_id": customer_id,
                "total_amount": total_amount,
                "status": status
            }, 201)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi tạo báo giá: " + str(e))
        finally:
            conn.close()

    def handle_get_quote_by_id(self, quote_id):
        conn = get_db_connection()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM quotes WHERE quote_id = ?;", (quote_id,)).fetchone()
        conn.close()
        if not row:
            self.send_error_json("Không tìm thấy báo giá", 404)
            return
        self.send_json({"success": True, "data": dict(row)})

    def handle_get_all_quotes(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT q.*, c.name as customer_name, c.phone as customer_phone FROM quotes q JOIN customers c ON q.customer_id = c.id ORDER BY q.quote_id DESC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_update_quote(self, quote_id, data):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            old = cur.execute("SELECT * FROM quotes WHERE quote_id = ?;", (quote_id,)).fetchone()
            if not old:
                self.send_error_json("Không tìm thấy báo giá", 404)
                return

            status = data.get('status', '').upper()
            if status and status in ('DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'EXPIRED'):
                cur.execute("UPDATE quotes SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE quote_id = ?;", (status, quote_id))
                log_audit(conn, 'quote', quote_id, 'STATUS_UPDATE', dict(old).get('status'), status, actor='consultant')
                conn.commit()
                sync_db_copies()

            self.send_json({"success": True, "message": "Đã cập nhật trạng thái báo giá"})
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi cập nhật báo giá: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # 5. ORDER & 6. PAYMENT (CHUẨN HÓA DỮ LIỆU ĐƠN HÀNG VÀ THANH TOÁN)
    # =========================================================================
    def handle_get_orders(self):
        """Admin lấy danh sách đơn hàng kèm thanh toán liên kết"""
        conn = get_db_connection()
        query = """
        SELECT 
            o.*,
            c.name as customer_name,
            c.phone as customer_phone,
            c.email as customer_email,
            c.address as customer_address,
            p.name as product_name,
            p.product_type,
            p.stock_quantity as remaining_stock,
            pm.payment_id,
            pm.status as payment_status,
            pm.qr_content,
            pm.received_amount,
            pm.expected_amount,
            pm.transaction_id,
            pm.transaction_code,
            pm.transaction_time,
            pm.raw_content
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        LEFT JOIN products p ON o.product_id = p.id
        LEFT JOIN payments pm ON o.id = pm.order_id
        ORDER BY o.id DESC;
        """
        rows = conn.execute(query).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_get_order_by_id(self, order_id):
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
        SELECT 
            o.*,
            c.name as customer_name,
            c.phone as customer_phone,
            c.email as customer_email,
            c.address as customer_address,
            p.name as product_name,
            p.product_type,
            pm.payment_id,
            pm.status as payment_status,
            pm.qr_content,
            pm.received_amount,
            pm.transaction_code
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        LEFT JOIN products p ON o.product_id = p.id
        LEFT JOIN payments pm ON o.id = pm.order_id
        WHERE o.id = ? OR o.order_code = ?;
        """
        row = cur.execute(query, (order_id, order_id)).fetchone()
        conn.close()
        if not row:
            self.send_error_json("Không tìm thấy đơn hàng", 404)
            return

        order_data = dict(row)
        self.send_json({"success": True, "data": order_data})

    def handle_get_orders_by_phone(self, phone):
        """Khách hàng tra cứu lịch sử đơn hàng theo số điện thoại"""
        norm_phone = normalize_vietnam_phone(phone)
        if not norm_phone:
            self.send_error_json("Số điện thoại không hợp lệ", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        query = """
        SELECT 
            o.id as order_id,
            o.order_code,
            o.amount,
            o.total_amount,
            o.status,
            o.created_at,
            o.purchased_at,
            p.name as product_name,
            p.product_type,
            c.name as customer_name,
            c.phone as customer_phone,
            pm.status as payment_status,
            pm.qr_content
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        LEFT JOIN products p ON o.product_id = p.id
        LEFT JOIN payments pm ON o.id = pm.order_id
        WHERE c.phone = ?
        ORDER BY o.id DESC;
        """
        rows = cur.execute(query, (norm_phone,)).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_create_order(self, data):
        """
        Tạo đơn hàng chuẩn kiến trúc:
        CUSTOMER -> QUOTE (tùy chọn) -> ORDER -> PAYMENT (tự động liên kết).
        Tự động liên kết Customer, sinh mã order_code, tạo Payment PENDING.
        """
        customer_name = str(data.get('customer_name') or data.get('full_name') or data.get('name') or '').strip()
        customer_phone = str(data.get('customer_phone') or data.get('phone') or '').strip()

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Tìm hoặc tạo khách hàng trung tâm
            cust_id = data.get('customer_id')
            if customer_phone and is_valid_vietnam_phone(customer_phone):
                cust, _ = find_or_create_customer(
                    conn, customer_phone, customer_name or f"Khách {customer_phone}",
                    email=data.get('customer_email') or data.get('email'),
                    address=data.get('customer_address') or data.get('address'),
                    actor='customer'
                )
                cust_id = cust['id']
            elif not cust_id:
                self.send_error_json("Thiếu thông tin khách hàng (customer_id hoặc customer_phone hợp lệ)", 400)
                return

            # Lấy thông tin khách hàng đầy đủ
            cust_row = cur.execute("SELECT * FROM customers WHERE id = ?;", (cust_id,)).fetchone()
            if not cust_row:
                self.send_error_json("Khách hàng không tồn tại", 400)
                return
            cust_info = dict(cust_row)

            # 2. Xác định sản phẩm & mức phí
            product_id = data.get('product_id')
            prod_row = None
            if product_id:
                prod_row = cur.execute("SELECT * FROM products WHERE id = ?;", (product_id,)).fetchone()
            if not prod_row:
                prod_name = str(data.get('product_name') or '').strip()
                if prod_name:
                    prod_row = cur.execute("SELECT * FROM products WHERE name LIKE ?;", (f"%{prod_name[:15]}%",)).fetchone()
            if not prod_row:
                prod_row = cur.execute("SELECT * FROM products LIMIT 1;").fetchone()

            prod_dict = dict(prod_row)
            p_id = prod_dict['id']
            p_name = prod_dict['name']
            p_type = prod_dict['product_type']
            p_stock = prod_dict['stock_quantity']

            amount = data.get('total_amount') or data.get('amount')
            if amount is None or amount == '':
                amount = prod_dict['price']
            amount = float(amount)
            if amount < 0:
                self.send_error_json("Số tiền đơn hàng không được âm", 400)
                return

            status = str(data.get('status') or 'pending').lower()
            if status not in ('pending', 'paid', 'processing', 'completed', 'cancelled', 'waiting_confirm', 'pending_payment'):
                status = 'pending'

            # 3. Sinh mã đơn hàng chuẩn (order_code)
            order_code = data.get('order_code')
            if not order_code:
                date_str = datetime.now().strftime('%y%m%d')
                cur.execute("SELECT COUNT(*) FROM orders;")
                order_seq = cur.fetchone()[0] + 1
                order_code = f"PJ{date_str}{str(order_seq).padStart(3, '0') if hasattr(str, 'padStart') else str(order_seq).zfill(3)}"

            # 4. Trừ kho nếu là sản phẩm vật lý và đơn đã thanh toán
            stock_deducted = False
            if status in ('paid', 'completed') and p_type == 'physical':
                if p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
                    stock_deducted = True

            # Thời hạn thanh toán (TTL 30 phút theo yêu cầu Phase 3)
            payment_deadline = data.get('payment_deadline')
            if not payment_deadline:
                payment_deadline = (datetime.now() + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')

            # 5. Lưu đơn hàng (orders)
            cur.execute("""
                INSERT INTO orders (
                    customer_id, product_id, amount, status, purchased_at,
                    order_code, quote_id, product_name, customer_name,
                    customer_phone, customer_address, insured_object,
                    vehicle_type, plate_number, insurance_start, insurance_end,
                    premium, discount, total_amount, payment_deadline, created_at
                ) VALUES (
                    ?, ?, ?, ?, CURRENT_TIMESTAMP,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, CURRENT_TIMESTAMP
                );
            """, (
                cust_id, p_id, amount, status,
                order_code, data.get('quote_id'), p_name, cust_info.get('full_name') or customer_name,
                cust_info.get('phone'), cust_info.get('address'), data.get('insured_object'),
                data.get('vehicle_type'), data.get('plate_number'), data.get('insurance_start'), data.get('insurance_end'),
                data.get('premium') or amount, data.get('discount') or 0, amount, payment_deadline
            ))
            order_id = cur.lastrowid

            # 6. Tự động khởi tạo bản ghi Thanh Toán liên kết (payments)
            qr_content = f"SEVQR PJICO {order_code}"
            pm_status = 'SUCCESS' if status in ('paid', 'completed') else 'PENDING'
            cur.execute("""
                INSERT INTO payments (
                    order_id, expected_amount, received_amount, qr_content,
                    bank_name, account_number, status, created_at
                ) VALUES (?, ?, ?, ?, 'VietinBank', '106006104248', ?, CURRENT_TIMESTAMP);
            """, (order_id, amount, amount if pm_status == 'SUCCESS' else 0, qr_content, pm_status))
            payment_id = cur.lastrowid

            # Ghi vết kiểm toán
            log_audit(conn, 'order', order_id, 'CREATE', None, {'order_code': order_code, 'amount': amount, 'status': status}, actor='customer')
            log_audit(conn, 'payment', payment_id, 'CREATE', None, {'expected_amount': amount, 'status': pm_status}, actor='system')

            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": f"Đã khởi tạo đơn hàng #{order_code} thành công",
                "order_id": order_id,
                "order_code": order_code,
                "customer_id": cust_id,
                "amount": amount,
                "status": status,
                "stock_deducted": stock_deducted,
                "payment_deadline": payment_deadline,
                "payment": {
                    "payment_id": payment_id,
                    "bank": "VietinBank",
                    "account": "106006104248",
                    "holder": "NGUYEN HUY VINH",
                    "qr_content": qr_content,
                    "expected_amount": amount,
                    "status": pm_status,
                    "deadline": payment_deadline
                }
            }, 201)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi tạo đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_update_order(self, item_id, data):
        status = data.get('status')
        amount = data.get('amount') or data.get('total_amount')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            row = cur.execute("""
                SELECT o.status, o.product_id, p.product_type, p.stock_quantity, o.amount
                FROM orders o
                LEFT JOIN products p ON o.product_id = p.id
                WHERE o.id = ? OR o.order_code = ?;
            """, (item_id, item_id)).fetchone()

            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            old_status, prod_id, p_type, p_stock, old_amount = row
            target_id = item_id

            # Nếu đơn chuyển từ pending sang paid và là sản phẩm vật lý -> Trừ tồn kho 1
            if old_status != 'paid' and status in ('paid', 'completed') and p_type == 'physical':
                if p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (prod_id,))

            updates = []
            params = []
            if status:
                updates.append("status = ?")
                params.append(status)
            if amount is not None:
                updates.extend(["amount = ?", "total_amount = ?"])
                params.extend([float(amount), float(amount)])

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                sql = f"UPDATE orders SET {', '.join(updates)} WHERE id = ? OR order_code = ?;"
                params.extend([item_id, item_id])
                cur.execute(sql, tuple(params))

                # Đồng bộ trạng thái payment nếu có
                if status in ('paid', 'completed'):
                    cur.execute("""
                        UPDATE payments
                        SET status = 'SUCCESS', received_amount = expected_amount, updated_at = CURRENT_TIMESTAMP
                        WHERE order_id = ? OR order_id = (SELECT id FROM orders WHERE order_code = ?);
                    """, (item_id, item_id))

            log_audit(conn, 'order', item_id, 'STATUS_UPDATE', old_status, status, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã cập nhật đơn hàng thành công", "status": status})
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi cập nhật đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_delete_order(self, item_id):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM payments WHERE order_id = ?;", (item_id,))
            cur.execute("DELETE FROM orders WHERE id = ?;", (item_id,))
            log_audit(conn, 'order', item_id, 'DELETE', None, None, actor='admin')
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa đơn hàng thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa đơn hàng: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # BACKWARD COMPATIBILITY: PUBLIC CHECKOUT & SEPAY WEBHOOK
    # =========================================================================
    def handle_public_create_order(self, payload):
        """Hỗ trợ tương thích ngược cho form đặt mua /thanhtoan"""
        self.handle_create_order(payload)

    def handle_public_check_order(self, query_string):
        """Khách hàng kiểm tra trạng thái đơn hàng thời gian thực từ /thanhtoan"""
        params = urllib.parse.parse_qs(query_string)
        order_id = params.get('id', [None])[0]
        phone = params.get('phone', [None])[0]

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            if order_id:
                cur.execute("""
                    SELECT o.id, o.order_code, o.amount, o.status, o.purchased_at,
                           o.plate_number, o.customer_address, c.email as customer_email, o.payment_deadline, o.created_at, o.updated_at,
                           c.name as customer_name, c.phone as customer_phone,
                           p.name as product_name, p.product_type,
                           pm.status as payment_status, pm.received_amount, pm.expected_amount,
                           pm.transaction_code, pm.transaction_time
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.id
                    LEFT JOIN products p ON o.product_id = p.id
                    LEFT JOIN payments pm ON o.id = pm.order_id
                    WHERE o.id = ? OR o.order_code = ?;
                """, (order_id, order_id))
            elif phone:
                norm_phone = normalize_vietnam_phone(phone)
                cur.execute("""
                    SELECT o.id, o.order_code, o.amount, o.status, o.purchased_at,
                           o.plate_number, o.customer_address, c.email as customer_email, o.payment_deadline, o.created_at, o.updated_at,
                           c.name as customer_name, c.phone as customer_phone,
                           p.name as product_name, p.product_type,
                           pm.status as payment_status, pm.received_amount, pm.expected_amount,
                           pm.transaction_code, pm.transaction_time
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.id
                    LEFT JOIN products p ON o.product_id = p.id
                    LEFT JOIN payments pm ON o.id = pm.order_id
                    WHERE c.phone = ?
                    ORDER BY o.id DESC LIMIT 1;
                """, (norm_phone,))
            else:
                self.send_error_json("Thiếu tham số tra cứu (id hoặc phone)")
                return

            row = cur.fetchone()
            if not row:
                self.send_json({"success": False, "message": "Không tìm thấy đơn hàng"})
                return

            d = dict(row)
            o_status = str(d.get('status') or '').lower()
            pm_status = str(d.get('payment_status') or '').upper()
            rec_amt = float(d.get('received_amount') or 0)
            exp_amt = float(d.get('expected_amount') or d.get('amount') or 0)
            deadline_str = d.get('payment_deadline')

            # Kiểm tra thời hạn thanh toán (Expiration TTL)
            is_expired = False
            if deadline_str and o_status not in ['paid', 'completed', 'delivered', 'issued']:
                try:
                    deadline_dt = datetime.strptime(deadline_str[:19], '%Y-%m-%d %H:%M:%S')
                    if datetime.now() > deadline_dt:
                        is_expired = True
                        cur.execute("UPDATE orders SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (d['id'],))
                        conn.commit()
                        sync_db_copies()
                except Exception:
                    pass

            # Tính toán 4 trạng thái thanh toán chuẩn: SUCCESS, PENDING, MISMATCH, EXPIRED
            if o_status in ['paid', 'completed', 'delivered', 'issued'] or pm_status == 'SUCCESS':
                d['payment_result'] = 'SUCCESS'
            elif is_expired:
                d['payment_result'] = 'EXPIRED'
            elif rec_amt > 0 and rec_amt != exp_amt:
                d['payment_result'] = 'MISMATCH'
            else:
                d['payment_result'] = 'PENDING'

            d['is_expired'] = is_expired
            d['expected_amount'] = exp_amt
            d['received_amount'] = rec_amt
            self.send_json({"success": True, "data": d})
        except Exception as e:
            self.send_error_json("Lỗi tra cứu đơn: " + str(e))
        finally:
            conn.close()

    def handle_public_confirm_order(self, payload):
        """Xác nhận khách hàng đã chuyển tiền -> Cập nhật trạng thái 'paid' và payments sang 'SUCCESS'"""
        order_id = payload.get('id') or payload.get('order_id') or payload.get('order_code')
        phone = payload.get('phone') or payload.get('customer_phone')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            target_order = None
            if order_id:
                cur.execute("""
                    SELECT o.id, o.status, o.product_id, p.product_type, p.stock_quantity,
                           p.name as product_name, o.amount, c.name as customer_name, c.phone as customer_phone, o.order_code
                    FROM orders o
                    LEFT JOIN products p ON o.product_id = p.id
                    JOIN customers c ON o.customer_id = c.id
                    WHERE o.id = ? OR o.order_code = ?;
                """, (order_id, order_id))
                target_order = cur.fetchone()
            elif phone:
                norm_phone = normalize_vietnam_phone(phone)
                cur.execute("""
                    SELECT o.id, o.status, o.product_id, p.product_type, p.stock_quantity,
                           p.name as product_name, o.amount, c.name as customer_name, c.phone as customer_phone, o.order_code
                    FROM orders o
                    LEFT JOIN products p ON o.product_id = p.id
                    JOIN customers c ON o.customer_id = c.id
                    WHERE c.phone = ?
                    ORDER BY o.id DESC LIMIT 1;
                """, (norm_phone,))
                target_order = cur.fetchone()

            if not target_order:
                self.send_error_json("Không tìm thấy đơn hàng để xác nhận")
                return

            o_id, cur_status, p_id, p_type, p_stock, p_name, o_amount, c_name, c_phone, o_code = target_order

            if cur_status != 'paid':
                cur.execute("UPDATE orders SET status = 'paid', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (o_id,))
                if p_type == 'physical' and p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))

                # Cập nhật payments
                cur.execute("""
                    UPDATE payments
                    SET status = 'SUCCESS', received_amount = expected_amount, updated_at = CURRENT_TIMESTAMP
                    WHERE order_id = ?;
                """, (o_id,))

                log_audit(conn, 'order', o_id, 'STATUS_UPDATE', cur_status, 'paid', actor='customer')
                log_audit(conn, 'payment', o_id, 'PAYMENT_SUCCESS', None, o_amount, actor='customer')
                conn.commit()
                sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Xác nhận nhận tiền thành công",
                "order_id": o_id,
                "order_code": o_code,
                "status": "paid",
                "customer_name": c_name,
                "customer_phone": c_phone,
                "product_name": p_name,
                "amount": o_amount
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi xác nhận thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_sepay_webhook(self, payload):
        """Xử lý webhook biến động số dư SePay (VietinBank SEVQR) - Tuyệt đối không thay đổi cú pháp"""
        content = str(payload.get('content') or payload.get('description') or '').strip()
        amount = float(payload.get('transferAmount') or payload.get('amount') or 0)
        transaction_id = str(payload.get('id') or payload.get('transaction_id') or '').strip()
        transaction_code = str(payload.get('code') or payload.get('referenceCode') or '').strip()
        transaction_date = payload.get('transactionDate') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Kiểm tra idempotency (Chống xử lý trùng lặp giao dịch)
            if transaction_id:
                existing_tx = cur.execute("SELECT * FROM payments WHERE transaction_id = ?;", (transaction_id,)).fetchone()
                if existing_tx:
                    self.send_json({"success": True, "message": "Giao dịch này đã được ghi nhận trước đó (Idempotent)"})
                    return

            # 2. Khớp giao dịch (Transaction Matching)
            # Ưu tiên 1: Mã đơn hàng (order_code) dạng PJ...
            order_codes = [m.upper() for m in re.findall(r'\bPJ[A-Za-z0-9_-]+\b', content, re.IGNORECASE) if m.upper() != 'PJICO']
            phone_match = re.search(r'0[35789]\d{8}', content)
            target_order = None

            matched_code = None
            if order_codes:
                matched_code = order_codes[0]
                cur.execute("""
                    SELECT o.id, o.product_id, p.product_type, p.stock_quantity, o.amount, o.status, o.order_code
                    FROM orders o
                    LEFT JOIN products p ON o.product_id = p.id
                    WHERE o.order_code = ?
                    ORDER BY o.id DESC LIMIT 1;
                """, (matched_code,))
                target_order = cur.fetchone()

            # Ưu tiên 2: Fallback qua số điện thoại khách hàng
            if not target_order and phone_match:
                matched_phone = phone_match.group(0)
                cur.execute("""
                    SELECT o.id, o.product_id, p.product_type, p.stock_quantity, o.amount, o.status, o.order_code
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.id
                    LEFT JOIN products p ON o.product_id = p.id
                    WHERE c.phone = ?
                    ORDER BY o.id DESC LIMIT 1;
                """, (matched_phone,))
                target_order = cur.fetchone()

            order_updated = False
            print(f"DEBUG SePay: transaction_id={transaction_id}, content={content}, matched_code={matched_code}, target_order={target_order}")
            
            if target_order:
                o_id, p_id, p_type, p_stock, exp_amount, o_status, o_code = target_order
                exp_amount = float(exp_amount or 0)

                # Kiểm tra so khớp số tiền (Amount Validation)
                if amount >= exp_amount:
                    # THANH TOÁN THÀNH CÔNG (SUCCESS hoặc OVERPAYMENT)
                    if o_status != 'paid':
                        cur.execute("UPDATE orders SET status = 'paid', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (o_id,))
                        if p_type == 'physical' and p_stock is not None and p_stock > 0:
                            cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
                        order_updated = True

                    cur.execute("""
                        UPDATE payments
                        SET status = 'SUCCESS', received_amount = ?, transaction_id = ?,
                            transaction_code = ?, transaction_time = ?, raw_content = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE order_id = ?;
                    """, (amount, transaction_id if transaction_id else None, transaction_code if transaction_code else None, transaction_date, content, o_id))

                    if amount > exp_amount:
                        log_audit(conn, 'payment', o_id, 'OVERPAYMENT', None, {'expected': exp_amount, 'received': amount, 'excess': amount - exp_amount, 'tx': transaction_id}, actor='sepay')

                    log_audit(conn, 'order', o_id, 'STATUS_UPDATE', o_status, 'paid', actor='sepay')
                    log_audit(conn, 'payment', o_id, 'SEPAY_WEBHOOK_SUCCESS', None, {'amount': amount, 'tx': transaction_id}, actor='sepay')
                else:
                    # SỐ TIỀN THANH TOÁN THIẾU (MISMATCH) -> KHÔNG ĐƯỢC TỰ ĐỘNG PAID
                    cur.execute("""
                        UPDATE payments
                        SET status = 'MISMATCH', received_amount = ?, transaction_id = ?,
                            transaction_code = ?, transaction_time = ?, raw_content = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE order_id = ?;
                    """, (amount, transaction_id if transaction_id else None, transaction_code if transaction_code else None, transaction_date, content, o_id))

                    log_audit(conn, 'payment', o_id, 'UNDERPAYMENT_MISMATCH', None, {'expected': exp_amount, 'received': amount, 'missing': exp_amount - amount, 'tx': transaction_id}, actor='sepay')
            else:
                # GIAO DỊCH KHÔNG TỰ KHỚP -> ĐƯA VÀO TRẠNG THÁI MANUAL_REVIEW ĐỂ ADMIN ĐỐI SOÁT
                cur.execute("""
                    INSERT INTO payments (
                        order_id, expected_amount, received_amount, transaction_id,
                        transaction_code, transaction_time, bank_name, account_number,
                        raw_content, status, created_at
                    ) VALUES (
                        NULL, ?, ?, ?,
                        ?, ?, 'VietinBank', '106006104248',
                        ?, 'MANUAL_REVIEW', CURRENT_TIMESTAMP
                    );
                """, (amount, amount, transaction_id if transaction_id else None, transaction_code if transaction_code else None, transaction_date, content))
                pm_id = cur.lastrowid
                log_audit(conn, 'payment', pm_id, 'MANUAL_REVIEW_UNMATCHED', None, {'content': content, 'amount': amount, 'tx': transaction_id}, actor='sepay')

            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "SePay webhook processed", "updated": order_updated})
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi xử lý SePay webhook: " + str(e))
        finally:
            conn.close()

    def handle_get_stats(self):
        conn = get_db_connection()
        cur = conn.cursor()
        prods = cur.execute("SELECT COUNT(*) FROM products;").fetchone()[0]
        custs = cur.execute("SELECT COUNT(*) FROM customers;").fetchone()[0]
        orders = cur.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
        survs = cur.execute("SELECT COUNT(*) FROM surveys;").fetchone()[0] if cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='surveys';").fetchone() else 0
        apps = cur.execute("SELECT COUNT(*) FROM applications;").fetchone()[0] if cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='applications';").fetchone() else 0
        quotes = cur.execute("SELECT COUNT(*) FROM quotes;").fetchone()[0] if cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quotes';").fetchone() else 0
        revenue = cur.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status IN ('paid', 'completed');").fetchone()[0]
        conn.close()
        self.send_json({
            "success": True,
            "data": {
                "products_count": prods,
                "customers_count": custs,
                "orders_count": orders,
                "surveys_count": survs,
                "applications_count": apps,
                "quotes_count": quotes,
                "revenue": float(revenue)
            }
        })



    # =========================================================================
    # PHASE 3: PAYMENT STATUS, REOPEN EXPIRED, MANUAL REVIEW & ADMIN PAYMENTS
    # =========================================================================
    def handle_get_order_payment_status(self, order_id_or_code):
        """Lấy trạng thái thanh toán chuẩn hóa không lộ secret: GET /api/orders/:orderId/payment-status"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT o.id, o.order_code, o.amount, o.status, o.payment_deadline,
                       o.created_at, o.updated_at,
                       c.name as customer_name, c.phone as customer_phone,
                       pm.status as payment_status, pm.received_amount, pm.expected_amount,
                       pm.transaction_code, pm.transaction_id, pm.transaction_time, pm.qr_content
                FROM orders o
                JOIN customers c ON o.customer_id = c.id
                LEFT JOIN payments pm ON o.id = pm.order_id
                WHERE o.id = ? OR o.order_code = ?
                ORDER BY o.id DESC LIMIT 1;
            """, (order_id_or_code, order_id_or_code))
            row = cur.fetchone()
            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            d = dict(row)
            o_status = str(d.get('status') or '').lower()
            pm_status = str(d.get('payment_status') or '').upper()
            deadline_str = d.get('payment_deadline')
            exp_amt = float(d.get('expected_amount') or d.get('amount') or 0)
            rec_amt = float(d.get('received_amount') or 0)

            # Kiểm tra hết hạn thanh toán
            is_expired = False
            if deadline_str and o_status not in ('paid', 'completed', 'delivered', 'issued'):
                try:
                    deadline_dt = datetime.strptime(deadline_str[:19], '%Y-%m-%d %H:%M:%S')
                    if datetime.now() > deadline_dt:
                        is_expired = True
                        cur.execute("UPDATE orders SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (d['id'],))
                        conn.commit()
                        sync_db_copies()
                except Exception:
                    pass

            # Xác định trạng thái chuẩn
            if o_status in ('paid', 'completed', 'delivered', 'issued') or pm_status == 'SUCCESS':
                std_status = 'PAID'
                std_payment_status = 'SUCCESS'
            elif is_expired:
                std_status = 'EXPIRED'
                std_payment_status = 'FAILED'
            elif rec_amt > 0 and rec_amt != exp_amt:
                std_status = 'MISMATCH'
                std_payment_status = 'MISMATCH'
            else:
                std_status = 'PENDING_PAYMENT'
                std_payment_status = pm_status or 'PENDING'

            resp = {
                "success": True,
                "orderId": d.get('order_code') or str(d.get('id')),
                "id": d.get('id'),
                "status": std_status,
                "expectedAmount": exp_amt,
                "receivedAmount": rec_amt,
                "paymentStatus": std_payment_status,
                "transactionTime": d.get('transaction_time'),
                "transactionId": d.get('transaction_id'),
                "deadline": deadline_str,
                "isExpired": is_expired,
                "qrContent": d.get('qr_content') or f"SEVQR PJICO {d.get('order_code')}"
            }
            self.send_json(resp)
        except Exception as e:
            self.send_error_json("Lỗi đọc trạng thái thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_reopen_order_payment(self, order_id_or_code, payload):
        """Khách tạo lại thanh toán khi đơn hết hạn: POST /api/orders/:id/reopen-payment"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT o.id, o.order_code, o.amount, o.status, o.product_id, p.name as product_name
                FROM orders o
                LEFT JOIN products p ON o.product_id = p.id
                WHERE o.id = ? OR o.order_code = ?;
            """, (order_id_or_code, order_id_or_code))
            row = cur.fetchone()
            if not row:
                self.send_error_json("Không tìm thấy đơn hàng để tạo lại thanh toán", 404)
                return

            o_id, o_code, amount, cur_status, p_id, p_name = row
            if str(cur_status).lower() in ('paid', 'completed'):
                self.send_json({"success": True, "message": "Đơn hàng này đã thanh toán thành công", "status": "PAID"})
                return

            new_deadline = (datetime.now() + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
            cur.execute("""
                UPDATE orders
                SET status = 'pending', payment_deadline = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (new_deadline, o_id))

            # Cập nhật payment
            cur.execute("""
                UPDATE payments
                SET status = 'PENDING', updated_at = CURRENT_TIMESTAMP
                WHERE order_id = ?;
            """, (o_id,))

            log_audit(conn, 'order', o_id, 'REOPEN_PAYMENT', cur_status, 'PENDING_PAYMENT', actor='customer')
            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Đã gia hạn thời gian thanh toán thêm 30 phút",
                "order_id": o_id,
                "order_code": o_code,
                "deadline": new_deadline,
                "expected_amount": float(amount),
                "status": "PENDING_PAYMENT",
                "payment": {
                    "bank": "VietinBank",
                    "account": "106006104248",
                    "holder": "NGUYEN HUY VINH",
                    "qr_content": f"SEVQR PJICO {o_code}",
                    "expected_amount": float(amount),
                    "status": "PENDING",
                    "deadline": new_deadline
                }
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi gia hạn thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_admin_confirm_payment(self, order_id_or_code, payload):
        """Admin xác nhận thanh toán thủ công (Manual Review / Xác nhận tiền về)"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT o.id, o.order_code, o.status, o.product_id, p.product_type, p.stock_quantity,
                       o.amount, c.name as customer_name, c.phone as customer_phone
                FROM orders o
                LEFT JOIN products p ON o.product_id = p.id
                JOIN customers c ON o.customer_id = c.id
                WHERE o.id = ? OR o.order_code = ?;
            """, (order_id_or_code, order_id_or_code))
            target_order = cur.fetchone()
            if not target_order:
                self.send_error_json("Không tìm thấy đơn hàng để xác nhận", 404)
                return

            o_id, o_code, cur_status, p_id, p_type, p_stock, o_amount, c_name, c_phone = target_order
            admin_user = self.get_current_username() or 'admin'

            if str(cur_status).lower() not in ('paid', 'completed'):
                cur.execute("UPDATE orders SET status = 'paid', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (o_id,))
                if p_type == 'physical' and p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))

                pm_id = payload.get('payment_id')
                tx_id = payload.get('transaction_id')
                if pm_id:
                    cur.execute("""
                        UPDATE payments
                        SET order_id = ?, status = 'SUCCESS', updated_at = CURRENT_TIMESTAMP
                        WHERE payment_id = ?;
                    """, (o_id, pm_id))
                else:
                    cur.execute("""
                        UPDATE payments
                        SET status = 'SUCCESS', received_amount = expected_amount, updated_at = CURRENT_TIMESTAMP
                        WHERE order_id = ?;
                    """, (o_id,))

                log_audit(conn, 'order', o_id, 'ADMIN_MANUAL_PAID', cur_status, 'paid', actor=admin_user)
                log_audit(conn, 'payment', pm_id or o_id, 'ADMIN_MANUAL_SUCCESS', None, o_amount, actor=admin_user)
                conn.commit()
                sync_db_copies()

            self.send_json({
                "success": True,
                "message": f"Quản trị viên đã duyệt thanh toán thành công cho đơn hàng #{o_code}",
                "order_id": o_id,
                "order_code": o_code,
                "status": "paid",
                "customer_name": c_name
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi duyệt thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_admin_get_payments(self, query_string):
        """Danh sách thanh toán cho Dashboard Admin: GET /api/admin/payments"""
        params = urllib.parse.parse_qs(query_string)
        status_filter = params.get('status', [None])[0]

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            sql = """
                SELECT pm.payment_id, pm.order_id, pm.expected_amount, pm.received_amount,
                       pm.transaction_id, pm.transaction_code, pm.transaction_time,
                       pm.bank_name, pm.account_number, pm.qr_content, pm.raw_content,
                       pm.status as payment_status, pm.created_at, pm.updated_at,
                       o.order_code, o.status as order_status, o.product_name,
                       c.name as customer_name, c.phone as customer_phone
                FROM payments pm
                LEFT JOIN orders o ON pm.order_id = o.id
                LEFT JOIN customers c ON o.customer_id = c.id
            """
            sql_params = []
            if status_filter and status_filter.upper() != 'ALL':
                sql += " WHERE pm.status = ?"
                sql_params.append(status_filter.upper())

            sql += " ORDER BY pm.payment_id DESC LIMIT 200;"
            cur.execute(sql, tuple(sql_params))
            rows = [dict(r) for r in cur.fetchall()]
            self.send_json({"success": True, "count": len(rows), "data": rows})
        except Exception as e:
            self.send_error_json("Lỗi đọc danh sách thanh toán: " + str(e))
        finally:
            conn.close()



    # =========================================================================
    # PHASE 4: ORDER MANAGEMENT, CUSTOMER 360, TRACKING & ADVANCED ANALYTICS
    # =========================================================================

    def handle_get_orders(self):
        """
        Admin lấy danh sách đơn hàng với đầy đủ bộ lọc, tìm kiếm, KPI badges và phân trang (Section 1, 2, 6, 7, 21)
        """
        parsed_url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_url.query)

        search_q = params.get('search', [None])[0]
        status_filter = params.get('status', [None])[0]
        payment_filter = params.get('payment_status', [None])[0]
        product_filter = params.get('product_id', [None])[0]
        staff_filter = params.get('staff', [None])[0]
        from_date = params.get('from_date', [None])[0]
        to_date = params.get('to_date', [None])[0]
        min_amt = params.get('min_amount', [None])[0]
        max_amt = params.get('max_amount', [None])[0]

        try:
            page = max(1, int(params.get('page', [1])[0]))
            limit = min(500, max(1, int(params.get('limit', [100])[0])))
        except Exception:
            page = 1
            limit = 100
        offset = (page - 1) * limit

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Tính toán số lượng cho tất cả các KPI badges
            kpi_query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN UPPER(COALESCE(o.status, 'NEW')) = 'NEW' THEN 1 ELSE 0 END) as kpi_new,
                SUM(CASE WHEN UPPER(o.status) = 'CONSULTING' THEN 1 ELSE 0 END) as kpi_consulting,
                SUM(CASE WHEN UPPER(o.status) = 'QUOTED' THEN 1 ELSE 0 END) as kpi_quoted,
                SUM(CASE WHEN UPPER(o.status) = 'WAITING_CONFIRM' THEN 1 ELSE 0 END) as kpi_waiting_confirm,
                SUM(CASE WHEN UPPER(o.status) IN ('PENDING_PAYMENT', 'PENDING') THEN 1 ELSE 0 END) as kpi_pending_payment,
                SUM(CASE WHEN UPPER(o.status) IN ('PAID') THEN 1 ELSE 0 END) as kpi_paid,
                SUM(CASE WHEN UPPER(o.status) IN ('PROCESSING') THEN 1 ELSE 0 END) as kpi_processing,
                SUM(CASE WHEN UPPER(o.status) IN ('ISSUED', 'COMPLETED') THEN 1 ELSE 0 END) as kpi_issued,
                SUM(CASE WHEN UPPER(o.status) = 'DELIVERED' THEN 1 ELSE 0 END) as kpi_delivered,
                SUM(CASE WHEN UPPER(o.status) IN ('CANCELLED') THEN 1 ELSE 0 END) as kpi_cancelled,
                SUM(CASE WHEN UPPER(o.status) = 'REFUND_PENDING' THEN 1 ELSE 0 END) as kpi_refund_pending,
                SUM(CASE WHEN UPPER(o.status) = 'REFUNDED' THEN 1 ELSE 0 END) as kpi_refunded
            FROM orders o;
            """
            kpi_row = cur.execute(kpi_query).fetchone()
            kpi_dict = {
                'TOTAL': kpi_row['total'] or 0,
                'NEW': kpi_row['kpi_new'] or 0,
                'CONSULTING': kpi_row['kpi_consulting'] or 0,
                'QUOTED': kpi_row['kpi_quoted'] or 0,
                'WAITING_CONFIRM': kpi_row['kpi_waiting_confirm'] or 0,
                'PENDING_PAYMENT': kpi_row['kpi_pending_payment'] or 0,
                'PAID': kpi_row['kpi_paid'] or 0,
                'PROCESSING': kpi_row['kpi_processing'] or 0,
                'ISSUED': kpi_row['kpi_issued'] or 0,
                'DELIVERED': kpi_row['kpi_delivered'] or 0,
                'CANCELLED': kpi_row['kpi_cancelled'] or 0,
                'REFUND_PENDING': kpi_row['kpi_refund_pending'] or 0,
                'REFUNDED': kpi_row['kpi_refunded'] or 0
            }

            # 2. Xây dựng câu truy vấn có điều kiện lọc
            base_sql = """
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            LEFT JOIN products p ON o.product_id = p.id
            LEFT JOIN payments pm ON o.id = pm.order_id
            WHERE 1=1
            """
            sql_params = []

            # Tìm kiếm tổng hợp (Search - Section 6)
            if search_q:
                q_clean = f"%{search_q.strip()}%"
                base_sql += """ AND (
                    o.order_code LIKE ? OR 
                    c.phone LIKE ? OR 
                    c.name LIKE ? OR 
                    c.full_name LIKE ? OR 
                    o.plate_number LIKE ? OR 
                    pm.transaction_id LIKE ? OR
                    pm.transaction_code LIKE ?
                )"""
                sql_params.extend([q_clean, q_clean, q_clean, q_clean, q_clean, q_clean, q_clean])

            # Bộ lọc trạng thái đơn (Status Filter - Section 1 & 7)
            if status_filter and status_filter.upper() != 'ALL':
                std_st = OrderStatusRules.normalize_status(status_filter)
                if std_st == 'PENDING_PAYMENT':
                    base_sql += " AND UPPER(o.status) IN ('PENDING_PAYMENT', 'PENDING')"
                elif std_st == 'PAID':
                    base_sql += " AND UPPER(o.status) IN ('PAID')"
                elif std_st == 'PROCESSING':
                    base_sql += " AND UPPER(o.status) IN ('PROCESSING')"
                elif std_st == 'ISSUED':
                    base_sql += " AND UPPER(o.status) IN ('ISSUED', 'COMPLETED')"
                elif std_st == 'CANCELLED':
                    base_sql += " AND UPPER(o.status) IN ('CANCELLED')"
                else:
                    base_sql += " AND UPPER(o.status) = ?"
                    sql_params.append(std_st)

            # Bộ lọc trạng thái thanh toán (Payment Filter)
            if payment_filter and payment_filter.upper() != 'ALL':
                base_sql += " AND UPPER(COALESCE(pm.status, 'PENDING')) = ?"
                sql_params.append(payment_filter.upper())

            # Bộ lọc sản phẩm
            if product_filter and product_filter.upper() != 'ALL':
                base_sql += " AND o.product_id = ?"
                sql_params.append(int(product_filter))

            # Bộ lọc nhân viên phụ trách
            if staff_filter and staff_filter.upper() != 'ALL':
                base_sql += " AND o.staff_assigned LIKE ?"
                sql_params.append(f"%{staff_filter}%")

            # Bộ lọc khoảng ngày
            if from_date:
                base_sql += " AND date(o.created_at) >= date(?)"
                sql_params.append(from_date)
            if to_date:
                base_sql += " AND date(o.created_at) <= date(?)"
                sql_params.append(to_date)

            # Bộ lọc khoảng tiền
            if min_amt:
                base_sql += " AND o.amount >= ?"
                sql_params.append(float(min_amt))
            if max_amt:
                base_sql += " AND o.amount <= ?"
                sql_params.append(float(max_amt))

            # Đếm tổng kết quả thỏa điều kiện
            count_sql = "SELECT COUNT(*) " + base_sql
            total_filtered = cur.execute(count_sql, tuple(sql_params)).fetchone()[0]

            # Lấy dữ liệu với phân trang
            data_sql = """
            SELECT 
                o.id,
                o.order_code,
                o.customer_id,
                o.product_id,
                o.amount,
                o.total_amount,
                o.status,
                o.created_at,
                o.updated_at,
                o.purchased_at,
                o.insured_object,
                o.vehicle_type,
                o.plate_number,
                o.insurance_start,
                o.insurance_end,
                o.payment_deadline,
                o.staff_assigned,
                o.policy_number,
                o.policy_expiry_date,
                o.document_url,
                c.name as customer_name,
                c.full_name,
                c.phone as customer_phone,
                c.email as customer_email,
                c.address as customer_address,
                p.name as product_name,
                p.product_type,
                pm.payment_id,
                pm.status as payment_status,
                pm.expected_amount,
                pm.received_amount,
                pm.transaction_id,
                pm.transaction_code,
                pm.transaction_time,
                pm.raw_content
            """ + base_sql + " ORDER BY o.id DESC LIMIT ? OFFSET ?;"
            
            sql_params.extend([limit, offset])
            rows = cur.execute(data_sql, tuple(sql_params)).fetchall()

            orders_list = []
            for r in rows:
                d = dict(r)
                d['status_standard'] = OrderStatusRules.normalize_status(d.get('status'))
                d['customer_name'] = d.get('full_name') or d.get('customer_name')
                orders_list.append(d)

            # Trả về kèm KPIs và pagination
            self.send_json({
                "success": True,
                "data": orders_list,
                "total": total_filtered,
                "page": page,
                "limit": limit,
                "kpis": kpi_dict
            })
        except Exception as e:
            self.send_error_json("Lỗi tải danh sách đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_get_order_detail(self, order_id_or_code):
        """
        Chi tiết đơn hàng đầy đủ các Section: Khách hàng, Sản phẩm, Bảo hiểm, Báo giá, Thanh toán, Trạng thái, Lịch sử, Tài liệu (Section 3, 4)
        """
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Thông tin đơn hàng
            query = """
            SELECT 
                o.*,
                c.name as customer_name, c.full_name, c.phone as customer_phone,
                c.email as customer_email, c.address as customer_address,
                c.identity_no as customer_identity_no, c.tax_code as customer_tax_code,
                p.name as product_name, p.product_type, p.price as product_price, p.stock_quantity,
                pm.payment_id, pm.status as payment_status, pm.expected_amount, pm.received_amount,
                pm.transaction_id, pm.transaction_code, pm.transaction_time, pm.raw_content, pm.qr_content,
                pol.policy_id, pol.policy_number, pol.issue_date, pol.start_date as policy_start,
                pol.expiry_date as policy_expiry, pol.status as policy_status, pol.document_url as policy_doc
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            LEFT JOIN products p ON o.product_id = p.id
            LEFT JOIN payments pm ON o.id = pm.order_id
            LEFT JOIN policies pol ON o.id = pol.order_id
            WHERE o.id = ? OR o.order_code = ?;
            """
            row = cur.execute(query, (order_id_or_code, order_id_or_code)).fetchone()
            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            order = dict(row)
            order_id = order['id']
            order_code = order['order_code']

            # 2. Báo giá liên kết (Quote)
            quote = None
            if order.get('quote_id'):
                q_row = cur.execute("SELECT * FROM quotes WHERE quote_id = ?;", (order['quote_id'],)).fetchone()
                if q_row:
                    quote = dict(q_row)

            # 3. Lịch sử thay đổi (Audit Log - Section 18)
            audits = cur.execute("""
                SELECT * FROM audit_logs 
                WHERE (entity_type = 'order' AND entity_id = ?) 
                   OR (entity_type = 'order' AND entity_id = ?)
                   OR (entity_type = 'payment' AND entity_id = ?)
                ORDER BY audit_id ASC;
            """, (str(order_id), str(order_code), str(order.get('payment_id') or order_id))).fetchall()
            audit_list = [dict(a) for a in audits]

            # 4. Tạo timeline các bước (Section 4)
            timeline = []
            steps = OrderStatusRules.VALID_FLOW
            cur_norm = OrderStatusRules.normalize_status(order['status'])
            cur_idx = steps.index(cur_norm) if cur_norm in steps else -1

            # Lấy audit logs tương ứng với status updates
            status_logs = {a['new_value']: a for a in audit_list if a['action'] in ('CREATE', 'STATUS_UPDATE', 'STATUS_TRANSITION', 'SEPAY_WEBHOOK_SUCCESS', 'ADMIN_MANUAL_PAID')}

            for idx, step in enumerate(steps):
                matched_log = status_logs.get(step)
                timeline.append({
                    "step": step,
                    "completed": idx <= cur_idx if cur_idx >= 0 else False,
                    "current": step == cur_norm,
                    "time": matched_log['timestamp'] if matched_log else None,
                    "actor": matched_log['actor'] if matched_log else None
                })

            # 5. Danh sách thông báo đã gửi
            notifs = cur.execute("SELECT * FROM notifications WHERE order_id = ? ORDER BY notification_id DESC;", (order_id,)).fetchall()
            notif_list = [dict(n) for n in notifs]

            self.send_json({
                "success": True,
                "data": {
                    "order": order,
                    "quote": quote,
                    "timeline": timeline,
                    "audit_logs": audit_list,
                    "notifications": notif_list,
                    "standard_status": cur_norm
                }
            })
        except Exception as e:
            self.send_error_json("Lỗi đọc chi tiết đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_order_transition(self, order_id_or_code, payload):
        """
        Chuyển trạng thái đơn hàng tuân thủ state machine và RBAC (Section 14 & 17)
        """
        username, user_role = self.get_current_user_info()
        next_status = payload.get('next_status') or payload.get('status')
        reason = payload.get('reason', '').strip()
        assigned_staff = payload.get('staff_assigned')

        if not next_status:
            self.send_error_json("Thiếu trạng thái tiếp theo (next_status)", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            row = cur.execute("""
                SELECT o.id, o.order_code, o.status, o.customer_id, o.amount, o.staff_assigned,
                       c.phone, c.name, pm.status as payment_status
                FROM orders o
                JOIN customers c ON o.customer_id = c.id
                LEFT JOIN payments pm ON o.id = pm.order_id
                WHERE o.id = ? OR o.order_code = ?;
            """, (order_id_or_code, order_id_or_code)).fetchone()

            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            o_id, o_code, cur_status, cust_id, o_amount, cur_staff, c_phone, c_name, pm_status = row
            cur_norm = OrderStatusRules.normalize_status(cur_status)
            nxt_norm = OrderStatusRules.normalize_status(next_status)
            is_admin = (user_role == 'ADMIN')
            is_paid = (str(cur_status).lower() == 'paid' or cur_norm in ['PAID', 'PROCESSING', 'ISSUED', 'DELIVERED'] or pm_status == 'SUCCESS')

            # Kiểm tra phân quyền RBAC (Section 17)
            if user_role == 'SALES':
                if nxt_norm in ['PAID', 'PROCESSING', 'ISSUED', 'DELIVERED', 'REFUNDED']:
                    self.send_error_json(f"Nhân viên SALES không có quyền chuyển đơn hàng sang trạng thái {nxt_norm}", 403)
                    return
            elif user_role == 'ACCOUNTING':
                if nxt_norm in ['ISSUED', 'DELIVERED']:
                    self.send_error_json(f"Bộ phận KẾ TOÁN không có quyền cấp đơn ({nxt_norm})", 403)
                    return
            elif user_role == 'PROCESSING':
                if nxt_norm in ['PAID']:
                    self.send_error_json(f"Bộ phận XỬ LÝ KHÔNG có quyền thay đổi trạng thái thanh toán ({nxt_norm})", 403)
                    return

            # Kiểm tra luật chuyển đổi State Machine (Section 14 & 15)
            can_go, msg = OrderStatusRules.can_transition(cur_status, next_status, is_admin=is_admin, is_paid=is_paid)
            if not can_go:
                self.send_error_json(msg, 400)
                return

            # Cập nhật đơn hàng
            updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
            params = [nxt_norm]
            if assigned_staff:
                updates.append("staff_assigned = ?")
                params.append(assigned_staff)
            params.append(o_id)

            cur.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = ?;", tuple(params))

            # Ghi audit log
            log_audit(conn, 'order', o_id, 'STATUS_TRANSITION', cur_norm, nxt_norm, actor=username or 'admin', reason=reason)

            # Gửi thông báo tự động tương ứng theo event (Section 10)
            if nxt_norm == 'PROCESSING':
                NotificationService.send(conn, 'ORDER_PROCESSING', c_phone, {'order_code': o_code, 'db_order_id': o_id, 'customer_id': cust_id})
            elif nxt_norm == 'DELIVERED':
                NotificationService.send(conn, 'POLICY_DELIVERED', c_phone, {'order_code': o_code, 'db_order_id': o_id, 'customer_id': cust_id})

            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": f"Chuyển trạng thái đơn hàng #{o_code} thành {nxt_norm} thành công",
                "old_status": cur_norm,
                "new_status": nxt_norm
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi chuyển trạng thái: " + str(e))
        finally:
            conn.close()

    def handle_issue_policy(self, order_id_or_code, payload):
        """
        Cấp đơn bảo hiểm điện tử & sinh số GCN (Section 2, 14, 16)
        """
        username, user_role = self.get_current_user_info()
        if user_role not in ('ADMIN', 'PROCESSING'):
            self.send_error_json("Chỉ ADMIN hoặc bộ phận XỬ LÝ (PROCESSING) mới có quyền cấp đơn bảo hiểm", 403)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            row = cur.execute("""
                SELECT o.id, o.order_code, o.status, o.customer_id, o.amount, o.product_name,
                       o.insured_object, o.plate_number, o.insurance_start, o.insurance_end,
                       c.phone, c.name, pm.status as payment_status
                FROM orders o
                JOIN customers c ON o.customer_id = c.id
                LEFT JOIN payments pm ON o.id = pm.order_id
                WHERE o.id = ? OR o.order_code = ?;
            """, (order_id_or_code, order_id_or_code)).fetchone()

            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            o_id, o_code, cur_status, cust_id, amount, prod_name, ins_obj, plate, ins_start, ins_end, phone, c_name, pm_status = row
            cur_norm = OrderStatusRules.normalize_status(cur_status)

            # Ràng buộc: Không được cấp đơn nếu chưa thanh toán (Section 14)
            if cur_norm not in ['PAID', 'PROCESSING'] and pm_status != 'SUCCESS':
                self.send_error_json("Không thể cấp đơn bảo hiểm: Đơn hàng chưa được thanh toán (PAID)!", 400)
                return

            # Sinh số GCN duy nhất: PJ-HG-YYYYMMDD-XXXX
            today_str = datetime.now().strftime('%Y%m%d')
            rand_hex = secrets.token_hex(2).upper()
            policy_number = f"PJ-HG-{today_str}-{rand_hex}"

            # Tính ngày hiệu lực & hết hạn (Section 16 - Renewal Ready)
            now = datetime.now()
            start_dt = datetime.strptime(ins_start[:10], '%Y-%m-%d') if ins_start else now
            expiry_dt = start_dt + timedelta(days=365)
            expiry_str = expiry_dt.strftime('%Y-%m-%d %H:%M:%S')
            start_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
            issue_date = now.strftime('%Y-%m-%d %H:%M:%S')

            doc_url = f"/api/documents/{policy_number}/download"

            # Lưu vào bảng policies
            cur.execute("""
                INSERT INTO policies (
                    policy_number, order_id, customer_id, product_name, insured_object,
                    plate_number, issue_date, start_date, expiry_date, premium, status,
                    document_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, CURRENT_TIMESTAMP);
            """, (policy_number, o_id, cust_id, prod_name or 'Bảo hiểm PJICO', ins_obj or plate or 'Phương tiện', plate, issue_date, start_str, expiry_str, amount, doc_url))

            # Cập nhật order
            cur.execute("""
                UPDATE orders 
                SET status = 'ISSUED', policy_number = ?, policy_expiry_date = ?, document_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (policy_number, expiry_str, doc_url, o_id))

            log_audit(conn, 'order', o_id, 'POLICY_ISSUED', cur_norm, 'ISSUED', actor=username or 'admin', reason=f"Cấp GCN {policy_number}")
            log_audit(conn, 'policy', policy_number, 'CREATE', None, {'order_id': o_id, 'expiry': expiry_str}, actor=username or 'admin')

            # Gửi thông báo đến khách hàng (Section 10 & 11)
            NotificationService.send(conn, 'POLICY_ISSUED', phone, {
                'order_code': o_code, 'db_order_id': o_id, 'customer_id': cust_id, 'policy_number': policy_number
            })

            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": f"Đã cấp Giấy chứng nhận bảo hiểm thành công: {policy_number}",
                "policy_number": policy_number,
                "order_code": o_code,
                "expiry_date": expiry_str,
                "document_url": doc_url
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi cấp đơn: " + str(e))
        finally:
            conn.close()

    def handle_deliver_policy(self, order_id_or_code, payload):
        """Đánh dấu đã giao GCN đến khách hàng (Section 2, 4)"""
        username, user_role = self.get_current_user_info()
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            row = cur.execute("""
                SELECT o.id, o.order_code, o.status, o.customer_id, c.phone
                FROM orders o JOIN customers c ON o.customer_id = c.id
                WHERE o.id = ? OR o.order_code = ?;
            """, (order_id_or_code, order_id_or_code)).fetchone()

            if not row:
                self.send_error_json("Không tìm thấy đơn hàng", 404)
                return

            o_id, o_code, cur_status, cust_id, phone = row
            cur.execute("UPDATE orders SET status = 'DELIVERED', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (o_id,))
            log_audit(conn, 'order', o_id, 'STATUS_TRANSITION', cur_status, 'DELIVERED', actor=username or 'admin', reason="Đã bàn giao khách hàng")

            NotificationService.send(conn, 'POLICY_DELIVERED', phone, {
                'order_code': o_code, 'db_order_id': o_id, 'customer_id': cust_id
            })

            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": f"Đã cập nhật trạng thái đơn #{o_code} sang DELIVERED"})
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi giao đơn: " + str(e))
        finally:
            conn.close()

    def handle_get_customer_profile(self, customer_id):
        """Hồ sơ khách hàng 360 độ: Master info, khảo sát, báo giá, đơn hàng, thanh toán, hợp đồng (Section 5)"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cust_row = cur.execute("SELECT * FROM customers WHERE id = ? OR phone = ?;", (customer_id, customer_id)).fetchone()
            if not cust_row:
                self.send_error_json("Không tìm thấy khách hàng", 404)
                return

            cust = dict(cust_row)
            c_id = cust['id']

            # Lịch sử khảo sát
            surveys = [dict(r) for r in cur.execute("SELECT * FROM surveys WHERE customer_id = ? ORDER BY survey_id DESC;", (c_id,)).fetchall()]

            # Lịch sử giấy yêu cầu
            apps = [dict(r) for r in cur.execute("SELECT * FROM applications WHERE customer_id = ? ORDER BY application_id DESC;", (c_id,)).fetchall()]

            # Lịch sử báo giá
            quotes = [dict(r) for r in cur.execute("SELECT * FROM quotes WHERE customer_id = ? ORDER BY quote_id DESC;", (c_id,)).fetchall()]

            # Lịch sử đơn hàng kèm thanh toán
            orders = [dict(r) for r in cur.execute("""
                SELECT o.*, pm.status as payment_status, pm.transaction_id, pm.received_amount
                FROM orders o LEFT JOIN payments pm ON o.id = pm.order_id
                WHERE o.customer_id = ? ORDER BY o.id DESC;
            """, (c_id,)).fetchall()]

            # Lịch sử Giấy chứng nhận bảo hiểm
            policies = [dict(r) for r in cur.execute("SELECT * FROM policies WHERE customer_id = ? ORDER BY policy_id DESC;", (c_id,)).fetchall()]

            # Các sản phẩm quan tâm (Distinct products)
            interested_products = []
            seen_prods = set()
            for o in orders:
                if o.get('product_name') and o['product_name'] not in seen_prods:
                    seen_prods.add(o['product_name'])
                    interested_products.append(o['product_name'])
            for s in surveys:
                if s.get('product_name') and s['product_name'] not in seen_prods:
                    seen_prods.add(s['product_name'])
                    interested_products.append(s['product_name'])

            self.send_json({
                "success": True,
                "data": {
                    "customer": cust,
                    "interested_products": interested_products,
                    "surveys": surveys,
                    "applications": apps,
                    "quotes": quotes,
                    "orders": orders,
                    "policies": policies
                }
            })
        except Exception as e:
            self.send_error_json("Lỗi đọc hồ sơ khách hàng: " + str(e))
        finally:
            conn.close()

    def handle_public_track_order(self, query_string):
        """
        Tra cứu đơn hàng công khai bắt buộc cả SĐT và Mã đơn (Section 12)
        Tuyệt đối không cho tra cứu chỉ bằng Order ID đơn lẻ nhằm bảo vệ dữ liệu khách hàng.
        """
        params = urllib.parse.parse_qs(query_string)
        order_code = params.get('order', params.get('order_code', [None]))[0]
        phone = params.get('phone', [None])[0]

        if not order_code or not phone:
            self.send_error_json("Để bảo mật, vui lòng nhập chính xác cả Số điện thoại và Mã đơn hàng.", 400)
            return

        norm_phone = normalize_vietnam_phone(phone)
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            query = """
            SELECT 
                o.id, o.order_code, o.amount, o.status, o.created_at, o.purchased_at,
                o.plate_number, o.insured_object, o.insurance_start, o.insurance_end,
                o.policy_number, o.policy_expiry_date, o.document_url,
                p.name as product_name, p.product_type,
                pm.status as payment_status, pm.received_amount, pm.expected_amount,
                c.name as customer_name, c.phone as customer_phone
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            LEFT JOIN products p ON o.product_id = p.id
            LEFT JOIN payments pm ON o.id = pm.order_id
            WHERE (o.order_code = ? OR o.id = ?) AND c.phone = ?;
            """
            row = cur.execute(query, (order_code.strip(), order_code.strip(), norm_phone)).fetchone()
            if not row:
                self.send_error_json("Không tìm thấy đơn hàng khớp với Số điện thoại và Mã đơn đã cung cấp.", 404)
                return

            d = dict(row)
            std_status = OrderStatusRules.normalize_status(d.get('status'))

            # Tạo timeline tiến trình trực quan
            timeline_steps = [
                {"key": "NEW", "title": "Tiếp nhận đơn", "desc": "Đơn hàng đã được khởi tạo"},
                {"key": "PENDING_PAYMENT", "title": "Chờ thanh toán", "desc": "Chờ chuyển khoản VietQR"},
                {"key": "PAID", "title": "Đã thanh toán", "desc": "PJICO đã nhận tiền thành công"},
                {"key": "PROCESSING", "title": "Đang cấp đơn", "desc": "Đang kiểm tra & cấp GCN điện tử"},
                {"key": "ISSUED", "title": "Đã cấp bảo hiểm", "desc": "GCN điện tử đã phát hành"},
                {"key": "DELIVERED", "title": "Hoàn tất", "desc": "Hồ sơ đã gửi tới khách hàng"}
            ]

            cur_order_idx = 0
            if std_status in ['PENDING_PAYMENT', 'WAITING_CONFIRM']:
                cur_order_idx = 1
            elif std_status == 'PAID':
                cur_order_idx = 2
            elif std_status == 'PROCESSING':
                cur_order_idx = 3
            elif std_status in ['ISSUED', 'COMPLETED']:
                cur_order_idx = 4
            elif std_status == 'DELIVERED':
                cur_order_idx = 5

            steps_status = []
            for i, st in enumerate(timeline_steps):
                steps_status.append({
                    "step": st['key'],
                    "title": st['title'],
                    "desc": st['desc'],
                    "done": i <= cur_order_idx,
                    "current": i == cur_order_idx
                })

            # Ẩn bớt họ tên nhạy cảm: "Nguyễn Văn A" -> "Nguyễn V***"
            raw_name = d.get('customer_name') or ''
            words = raw_name.split()
            masked_name = (words[0] + " " + words[-1][0] + "***") if len(words) > 1 else raw_name

            res = {
                "success": True,
                "order_code": d['order_code'],
                "customer_name_masked": masked_name,
                "product_name": d.get('product_name'),
                "amount": float(d.get('amount') or 0),
                "created_at": d.get('created_at'),
                "status": std_status,
                "payment_status": d.get('payment_status') or 'PENDING',
                "timeline": steps_status,
                "has_policy": bool(d.get('policy_number')),
                "policy_number": d.get('policy_number'),
                "policy_expiry": d.get('policy_expiry_date'),
                "certificate_url": f"/api/documents/{d['order_code']}/download?phone={norm_phone}" if d.get('policy_number') else None
            }
            self.send_json(res)
        except Exception as e:
            self.send_error_json("Lỗi tra cứu: " + str(e))
        finally:
            conn.close()

    def handle_download_document(self, identifier, query_string):
        """
        Tải / Xem Giấy chứng nhận bảo hiểm an toàn có kiểm tra quyền (Section 13)
        Yêu cầu token Admin HOẶC khớp số điện thoại của đơn hàng.
        """
        params = urllib.parse.parse_qs(query_string)
        phone = params.get('phone', [None])[0]
        norm_phone = normalize_vietnam_phone(phone) if phone else None
        is_auth = self.is_authenticated()

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Tra cứu theo policy_number hoặc order_code hoặc id
            query = """
            SELECT pol.*, o.order_code, o.amount, o.insured_object, o.plate_number,
                   c.name as customer_name, c.phone as customer_phone, c.address as customer_address,
                   c.identity_no as customer_identity
            FROM policies pol
            JOIN orders o ON pol.order_id = o.id
            JOIN customers c ON pol.customer_id = c.id
            WHERE pol.policy_number = ? OR o.order_code = ? OR o.id = ?;
            """
            row = cur.execute(query, (identifier, identifier, identifier)).fetchone()
            if not row:
                self.send_error_json("Không tìm thấy Giấy chứng nhận bảo hiểm tương ứng", 404)
                return

            pol = dict(row)

            # Kiểm tra quyền truy cập (Section 13)
            if not is_auth:
                if not norm_phone or norm_phone != pol.get('customer_phone'):
                    self.send_error_json("Quyền truy cập bị từ chối: Số điện thoại không khớp với người thụ hưởng bảo hiểm", 403)
                    return

            # Xuất tài liệu Giấy Chứng Nhận Điện Tử PJICO Hà Giang chuẩn
            html_doc = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>GIẤY CHỨNG NHẬN BẢO HIỂM ĐIỆN TỬ - PJICO HÀ GIANG</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; color: #1e293b; }}
        .cert-card {{ max-width: 800px; margin: 0 auto; background: white; border: 2px solid #005a9e; border-radius: 12px; padding: 40px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); position: relative; }}
        .header {{ text-align: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; }}
        .logo-title {{ font-size: 20px; font-weight: 800; color: #005a9e; letter-spacing: 1px; }}
        .company-sub {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
        .cert-title {{ font-size: 24px; font-weight: 800; color: #d97706; margin: 20px 0 5px 0; text-transform: uppercase; }}
        .cert-num {{ font-size: 15px; font-weight: 700; color: #005a9e; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 25px; }}
        .item {{ padding: 10px; background: #f8fafc; border-radius: 6px; border-left: 3px solid #005a9e; }}
        .label {{ font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; }}
        .value {{ font-size: 15px; font-weight: 700; color: #0f172a; margin-top: 3px; }}
        .stamp-box {{ margin-top: 30px; display: flex; justify-content: space-between; align-items: flex-end; }}
        .stamp {{ text-align: center; color: #b91c1c; border: 2px dashed #b91c1c; padding: 12px 20px; border-radius: 8px; font-weight: 800; }}
        .btn-print {{ margin-top: 20px; text-align: center; }}
        .btn {{ background: #005a9e; color: white; border: none; padding: 10px 24px; border-radius: 6px; font-weight: bold; cursor: pointer; }}
        @media print {{ .btn-print {{ display: none; }} body {{ background: white; padding: 0; }} .cert-card {{ box-shadow: none; border: 1px solid #ccc; }} }}
    </style>
</head>
<body>
    <div class="cert-card">
        <div class="header">
            <div class="logo-title">TỔNG CÔNG TY CỔ PHẦN BẢO HIỂM PETROLIMEX - PJICO</div>
            <div class="company-sub">CÔNG TY BẢO HIỂM PJICO HÀ GIANG • Hotline: 0987.501.199 • Website: pjicohagiang.xyz</div>
            <div class="cert-title">GIẤY CHỨNG NHẬN BẢO HIỂM ĐIỆN TỬ</div>
            <div class="cert-num">Số GCN: {pol['policy_number']}</div>
        </div>
        <div class="grid">
            <div class="item"><div class="label">Chủ hợp đồng / Khách hàng</div><div class="value">{pol['customer_name']}</div></div>
            <div class="item"><div class="label">Số điện thoại liên hệ</div><div class="value">{pol['customer_phone']}</div></div>
            <div class="item"><div class="label">Sản phẩm bảo hiểm</div><div class="value">{pol['product_name']}</div></div>
            <div class="item"><div class="label">Đối tượng / Biển số xe</div><div class="value">{pol['plate_number'] or pol['insured_object'] or 'Theo hợp đồng'}</div></div>
            <div class="item"><div class="label">Thời hạn hiệu lực từ</div><div class="value">{pol['start_date'][:10]}</div></div>
            <div class="item"><div class="label">Thời hạn hết hiệu lực</div><div class="value">{pol['expiry_date'][:10]}</div></div>
            <div class="item"><div class="label">Phí bảo hiểm đã nộp</div><div class="value">{pol['premium']:,.0f} VNĐ</div></div>
            <div class="item"><div class="label">Mã đơn hàng liên kết</div><div class="value">#{pol['order_code']}</div></div>
        </div>
        <div class="stamp-box">
            <div style="font-size: 12px; color: #64748b;">
                Ngày phát hành: {pol['issue_date'][:19]}<br>
                Xác thực điện tử qua mã QR / Hệ thống quản trị PJICO Hà Giang
            </div>
            <div class="stamp">
                PJICO HÀ GIANG<br>
                ĐÃ KÝ SỐ ĐIỆN TỬ
            </div>
        </div>
        <div class="btn-print">
            <button class="btn" onclick="window.print()">In / Lưu Chứng Nhận (PDF)</button>
        </div>
    </div>
</body>
</html>
"""
            body = html_doc.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error_json("Lỗi đọc tài liệu: " + str(e))
        finally:
            conn.close()

    def handle_admin_kpis(self):
        """Báo cáo chỉ số kinh doanh & tỷ lệ chuyển đổi phễu (Section 19)"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            month_str = datetime.now().strftime('%Y-%m')

            rev_today = cur.execute("""
                SELECT COALESCE(SUM(amount), 0) FROM orders 
                WHERE status IN ('PAID', 'paid', 'PROCESSING', 'processing', 'ISSUED', 'completed', 'DELIVERED')
                  AND date(created_at) = date(?);
            """, (today_str,)).fetchone()[0]

            rev_month = cur.execute("""
                SELECT COALESCE(SUM(amount), 0) FROM orders 
                WHERE status IN ('PAID', 'paid', 'PROCESSING', 'processing', 'ISSUED', 'completed', 'DELIVERED')
                  AND strftime('%Y-%m', created_at) = ?;
            """, (month_str,)).fetchone()[0]

            total_orders = cur.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
            paid_orders = cur.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE status IN ('PAID', 'paid', 'PROCESSING', 'processing', 'ISSUED', 'completed', 'DELIVERED');
            """).fetchone()[0]

            total_rev = cur.execute("""
                SELECT COALESCE(SUM(amount), 0) FROM orders 
                WHERE status IN ('PAID', 'paid', 'PROCESSING', 'processing', 'ISSUED', 'completed', 'DELIVERED');
            """).fetchone()[0]

            aov = (total_rev / paid_orders) if paid_orders > 0 else 0

            # Phễu chuyển đổi: Survey -> Quote -> Order -> Paid -> Issued -> Delivered
            surveys_cnt = cur.execute("SELECT COUNT(*) FROM surveys;").fetchone()[0]
            quotes_cnt = cur.execute("SELECT COUNT(*) FROM quotes;").fetchone()[0]
            orders_cnt = total_orders
            paid_cnt = paid_orders
            issued_cnt = cur.execute("SELECT COUNT(*) FROM orders WHERE status IN ('ISSUED', 'completed', 'DELIVERED');").fetchone()[0]
            delivered_cnt = cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'DELIVERED';").fetchone()[0]

            self.send_json({
                "success": True,
                "kpis": {
                    "revenue_today": float(rev_today),
                    "revenue_month": float(rev_month),
                    "total_orders": total_orders,
                    "paid_orders": paid_orders,
                    "average_order_value": round(aov, 0),
                    "funnel": {
                        "surveys": surveys_cnt,
                        "quotes": quotes_cnt,
                        "orders": orders_cnt,
                        "paid": paid_cnt,
                        "issued": issued_cnt,
                        "delivered": delivered_cnt,
                        "rates": {
                            "survey_to_quote": round((quotes_cnt / surveys_cnt * 100) if surveys_cnt > 0 else 0, 1),
                            "quote_to_order": round((orders_cnt / quotes_cnt * 100) if quotes_cnt > 0 else 0, 1),
                            "order_to_paid": round((paid_cnt / orders_cnt * 100) if orders_cnt > 0 else 0, 1),
                            "paid_to_issued": round((issued_cnt / paid_cnt * 100) if paid_cnt > 0 else 0, 1),
                            "issued_to_delivered": round((delivered_cnt / issued_cnt * 100) if issued_cnt > 0 else 0, 1)
                        }
                    }
                }
            })
        except Exception as e:
            self.send_error_json("Lỗi đọc số liệu KPI: " + str(e))
        finally:
            conn.close()

    def handle_admin_renewals(self):
        """Danh sách hợp đồng sắp hết hạn 30, 15, 7 ngày phục vụ tái tục (Section 16)"""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            today = datetime.now().date()
            rows = cur.execute("""
                SELECT pol.*, c.name as customer_name, c.phone as customer_phone, o.order_code
                FROM policies pol
                JOIN customers c ON pol.customer_id = c.id
                JOIN orders o ON pol.order_id = o.id
                WHERE pol.status = 'ACTIVE'
                ORDER BY pol.expiry_date ASC;
            """).fetchall()

            due_30 = []
            due_15 = []
            due_7 = []
            expired = []

            for r in rows:
                d = dict(r)
                try:
                    exp_date = datetime.strptime(d['expiry_date'][:10], '%Y-%m-%d').date()
                    days_left = (exp_date - today).days
                    d['days_left'] = days_left
                    if days_left < 0:
                        expired.append(d)
                    elif days_left <= 7:
                        due_7.append(d)
                    elif days_left <= 15:
                        due_15.append(d)
                    elif days_left <= 30:
                        due_30.append(d)
                except Exception:
                    pass

            self.send_json({
                "success": True,
                "renewals": {
                    "due_in_7_days": due_7,
                    "due_in_15_days": due_15,
                    "due_in_30_days": due_30,
                    "expired": expired,
                    "counts": {
                        "due_7": len(due_7),
                        "due_15": len(due_15),
                        "due_30": len(due_30),
                        "expired": len(expired)
                    }
                }
            })
        except Exception as e:
            self.send_error_json("Lỗi kiểm tra tái tục: " + str(e))
        finally:
            conn.close()

    def handle_admin_match_payment(self, payment_id, payload):
        """Khớp giao dịch Manual Review với mã đơn hàng (Section 9)"""
        username, user_role = self.get_current_user_info()
        if user_role not in ('ADMIN', 'ACCOUNTING'):
            self.send_error_json("Chỉ ADMIN hoặc KẾ TOÁN (ACCOUNTING) mới có quyền khớp thanh toán", 403)
            return

        order_code = payload.get('order_code') or payload.get('order_id')
        reason = payload.get('reason', 'Admin đối soát khớp giao dịch thủ công').strip()

        if not order_code:
            self.send_error_json("Vui lòng cung cấp mã đơn hàng (order_code) để khớp", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            pm_row = cur.execute("SELECT * FROM payments WHERE payment_id = ?;", (payment_id,)).fetchone()
            if not pm_row:
                self.send_error_json("Không tìm thấy giao dịch thanh toán", 404)
                return

            pm = dict(pm_row)
            order_row = cur.execute("""
                SELECT o.id, o.order_code, o.amount, o.status, c.phone, c.name, p.product_type, p.stock_quantity, o.product_id
                FROM orders o 
                JOIN customers c ON o.customer_id = c.id
                LEFT JOIN products p ON o.product_id = p.id
                WHERE o.order_code = ? OR o.id = ?;
            """, (order_code, order_code)).fetchone()

            if not order_row:
                self.send_error_json(f"Không tìm thấy đơn hàng #{order_code}", 404)
                return

            o_id, o_code, o_amount, o_status, c_phone, c_name, p_type, p_stock, p_id = order_row
            rec_amount = float(pm.get('received_amount') or 0)
            exp_amount = float(o_amount or 0)

            # Cập nhật payment
            new_pm_status = 'SUCCESS' if rec_amount >= exp_amount else 'MISMATCH'
            cur.execute("""
                UPDATE payments
                SET order_id = ?, status = ?, expected_amount = ?, updated_at = CURRENT_TIMESTAMP
                WHERE payment_id = ?;
            """, (o_id, new_pm_status, exp_amount, payment_id))

            # Nếu đủ tiền -> Đơn chuyển sang PAID
            old_order_status = o_status
            if new_pm_status == 'SUCCESS':
                cur.execute("UPDATE orders SET status = 'PAID', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (o_id,))
                if p_type == 'physical' and p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))

                # Gửi thông báo thành công
                NotificationService.send(conn, 'PAYMENT_SUCCESS', c_phone, {
                    'order_code': o_code, 'db_order_id': o_id
                })

            log_audit(conn, 'payment', payment_id, 'MANUAL_MATCH', pm.get('status'), new_pm_status, actor=username or 'admin', reason=reason)
            log_audit(conn, 'order', o_id, 'STATUS_UPDATE', old_order_status, 'PAID' if new_pm_status == 'SUCCESS' else old_order_status, actor=username or 'admin', reason=reason)

            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": f"Đã khớp giao dịch {payment_id} với đơn hàng #{o_code}",
                "payment_status": new_pm_status,
                "order_code": o_code
            })
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khớp thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_get_audit_logs(self, query_string):
        """Truy vấn nhật ký kiểm toán hệ thống (Section 18)"""
        params = urllib.parse.parse_qs(query_string)
        entity_type = params.get('entity_type', [None])[0]
        entity_id = params.get('entity_id', [None])[0]
        limit = min(200, max(1, int(params.get('limit', [50])[0])))

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            sql = "SELECT * FROM audit_logs WHERE 1=1"
            sql_params = []
            if entity_type:
                sql += " AND entity_type = ?"
                sql_params.append(entity_type)
            if entity_id:
                sql += " AND entity_id = ?"
                sql_params.append(str(entity_id))
            sql += " ORDER BY audit_id DESC LIMIT ?;"
            sql_params.append(limit)

            rows = cur.execute(sql, tuple(sql_params)).fetchall()
            self.send_json({"success": True, "count": len(rows), "data": [dict(r) for r in rows]})
        except Exception as e:
            self.send_error_json("Lỗi đọc audit log: " + str(e))
        finally:
            conn.close()

def run_server():
    init_database()
    handler = AdminRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"==================================================")
        print(f"  PJICO HA GIANG SERVER DANG CHAY TAI:")
        print(f"  - Landing Page: http://localhost:{PORT}")
        print(f"  - Admin Panel:  http://localhost:{PORT}/admin")
        print(f"  - Tai khoan:    admin")
        print(f"  - Mat khau:     pjico@2026")
        print(f"==================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nDang tat server...")
            httpd.server_close()

if __name__ == '__main__':
    run_server()
