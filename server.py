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

def log_audit(conn, entity_type, entity_id, action, old_value=None, new_value=None, actor='system'):
    """Ghi vết kiểm toán (Audit Log) theo dõi các thay đổi quan trọng"""
    try:
        cur = conn.cursor()
        old_str = json.dumps(old_value, ensure_ascii=False) if isinstance(old_value, (dict, list)) else (str(old_value) if old_value is not None else None)
        new_str = json.dumps(new_value, ensure_ascii=False) if isinstance(new_value, (dict, list)) else (str(new_value) if new_value is not None else None)
        cur.execute("""
            INSERT INTO audit_logs (entity_type, entity_id, action, old_value, new_value, actor, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
        """, (str(entity_type), str(entity_id), str(action), old_str, new_str, str(actor)))
    except Exception as e:
        print(f"Error logging audit: {e}")

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

        # 2. Public API Endpoints (Kiểm tra đơn, tra cứu khách hàng theo SĐT để auto-fill)
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

        self.send_json({
            "success": True,
            "message": "Đăng nhập thành công",
            "token": token,
            "user": {
                "id": user['id'],
                "username": user['username']
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
