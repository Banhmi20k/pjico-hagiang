"""
PJICO HÀ GIANG - ADMIN BACKEND SERVER & API (CÓ BẢO MẬT ĐĂNG NHẬP)
Phục vụ Landing Page tại '/' và Admin Panel tại '/admin'
Quản lý trực tiếp cơ sở dữ liệu SQLite 'brain.db'
Tài khoản mặc định: admin | Mật khẩu mặc định: pjico@2026
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
from datetime import datetime

# Đảm bảo in tiếng Việt trên Windows không bị lỗi cp1252
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

PORT = int(os.environ.get("PORT", 8080))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Quản lý phiên đăng nhập
ACTIVE_SESSIONS = set()

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

def init_database():
    """Khởi tạo các bảng và tài khoản quản trị mặc định"""
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

    # 3. Bảng khách hàng
    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE,
        zalo TEXT,
        registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 4. Bảng đơn hàng
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        amount NUMERIC NOT NULL CHECK (amount >= 0),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'paid', 'processing', 'completed', 'cancelled')),
        purchased_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT,
        FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
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
            ("Nguyễn Văn Bình", "0987501199", "0987501199"),
            ("Trần Thị Mai", "0912345678", "0912345678"),
            ("Hoàng Văn Đức", "0906069368", "0906069368")
        ]
        cur.executemany("INSERT INTO customers (name, phone, zalo) VALUES (?, ?, ?);", sample_custs)
        conn.commit()

    # Dữ liệu mẫu đơn hàng nếu rỗng
    order_count = cur.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
    if order_count == 0:
        sample_orders = [
            (1, 3, 86000, 'paid'),      # Khách 1 mua sản phẩm số
            (2, 1, 86000, 'completed')  # Khách 2 mua sản phẩm vật lý
        ]
        for c_id, p_id, amt, st in sample_orders:
            p_type, p_stock = cur.execute("SELECT product_type, stock_quantity FROM products WHERE id = ?;", (p_id,)).fetchone()
            if p_type == 'physical' and p_stock is not None and p_stock > 0:
                cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
            cur.execute("INSERT INTO orders (customer_id, product_id, amount, status) VALUES (?, ?, ?, ?);", (c_id, p_id, amt, st))
        conn.commit()

    conn.close()
    sync_db_copies()


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

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')

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

        if path == '/thanhtoan':
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

        # 2. API Endpoints bảo mật (Yêu cầu đăng nhập)
        if path in ('/api/stats', '/api/products', '/api/customers', '/api/orders', '/api/verify-token'):
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

        # Mặc định phục vụ static files (Landing page, ảnh...)
        super().do_GET()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')

        try:
            payload = self.get_parsed_body()
        except Exception as e:
            self.send_error_json("Dữ liệu JSON không hợp lệ: " + str(e))
            return

        # 1. Đăng nhập không cần token trước
        if path == '/api/login':
            self.handle_login(payload)
            return

        # 2. Tạo đơn hàng từ cổng thanh toán /thanhtoan
        if path == '/api/public/orders':
            self.handle_public_create_order(payload)
            return

        # 3. Webhook biến động số dư SePay (VietinBank SEVQR)
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
        elif path == '/api/customers':
            self.handle_create_customer(payload)
        elif path == '/api/orders':
            self.handle_create_order(payload)
        else:
            self.send_error_json("Endpoint không tồn tại", 404)

    def do_PUT(self):
        if not self.is_authenticated():
            self.send_error_json("Yêu cầu đăng nhập quản trị viên", 401)
            return

        parsed_url = urllib.parse.urlparse(self.path)
        parts = parsed_url.path.strip('/').split('/')

        try:
            payload = self.get_parsed_body()
        except Exception as e:
            self.send_error_json("Dữ liệu JSON không hợp lệ: " + str(e))
            return

        if len(parts) == 3 and parts[0] == 'api':
            resource = parts[1]
            try:
                item_id = int(parts[2])
            except ValueError:
                self.send_error_json("ID phải là số nguyên", 400)
                return

            if resource == 'products':
                self.handle_update_product(item_id, payload)
            elif resource == 'customers':
                self.handle_update_customer(item_id, payload)
            elif resource == 'orders':
                self.handle_update_order(item_id, payload)
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
        user = cur.execute("SELECT id, username, password_hash FROM admin_users WHERE username = ?;", (username,)).fetchone()
        conn.close()

        if not user or user['password_hash'] != hash_password(password):
            self.send_error_json("Tên đăng nhập hoặc mật khẩu không chính xác!", 401)
            return

        token = secrets.token_hex(24)
        ACTIVE_SESSIONS.add(token)

        self.send_json({
            "success": True,
            "message": "Đăng nhập thành công!",
            "token": token,
            "user": {
                "username": user['username']
            }
        })

    def handle_logout(self):
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ', 1)[1].strip()
            ACTIVE_SESSIONS.discard(token)
        self.send_json({"success": True, "message": "Đã đăng xuất"})

    def handle_change_password(self, data):
        old_pass = data.get('old_password', '').strip()
        new_pass = data.get('new_password', '').strip()
        username = data.get('username', 'admin').strip()

        if not old_pass or not new_pass:
            self.send_error_json("Vui lòng điền mật khẩu cũ và mật khẩu mới", 400)
            return
        if len(new_pass) < 6:
            self.send_error_json("Mật khẩu mới phải có tối thiểu 6 ký tự", 400)
            return

        conn = get_db_connection()
        cur = conn.cursor()
        user = cur.execute("SELECT id, username, password_hash FROM admin_users WHERE username = ?;", (username,)).fetchone()

        if not user or user['password_hash'] != hash_password(old_pass):
            conn.close()
            self.send_error_json("Mật khẩu hiện tại không đúng!", 400)
            return

        cur.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?;", (hash_password(new_pass), user['id']))
        conn.commit()
        conn.close()
        sync_db_copies()

        self.send_json({"success": True, "message": "Đổi mật khẩu thành công! Vui lòng sử dụng mật khẩu mới trong lần đăng nhập tới."})

    # =========================================================================
    # CÁC HÀM XỬ LÝ SẢN PHẨM (PRODUCTS)
    # =========================================================================
    def handle_get_products(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_create_product(self, data):
        name = data.get('name', '').strip()
        product_type = data.get('product_type', 'digital').strip()
        price = data.get('price', 0)
        description = data.get('description', '').strip()
        stock_quantity = data.get('stock_quantity')

        if not name:
            self.send_error_json("Tên sản phẩm không được để trống")
            return
        if product_type not in ('physical', 'digital', 'service'):
            self.send_error_json("Loại sản phẩm phải là physical, digital hoặc service")
            return
        try:
            price = float(price)
            if price < 0:
                raise ValueError()
        except ValueError:
            self.send_error_json("Giá sản phẩm phải là số không âm")
            return

        if product_type == 'physical':
            try:
                stock_quantity = int(stock_quantity) if stock_quantity is not None else 0
                if stock_quantity < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self.send_error_json("Sản phẩm vật lý bắt buộc phải có số lượng tồn kho >= 0")
                return
        else:
            stock_quantity = None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO products (name, product_type, price, description, stock_quantity) VALUES (?, ?, ?, ?, ?);",
                (name, product_type, price, description, stock_quantity)
            )
            new_id = cur.lastrowid
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã thêm sản phẩm thành công", "id": new_id})
        except Exception as e:
            self.send_error_json("Lỗi khi thêm sản phẩm: " + str(e))
        finally:
            conn.close()

    def handle_update_product(self, item_id, data):
        name = data.get('name', '').strip()
        product_type = data.get('product_type', 'digital').strip()
        price = data.get('price', 0)
        description = data.get('description', '').strip()
        stock_quantity = data.get('stock_quantity')

        if not name:
            self.send_error_json("Tên sản phẩm không được để trống")
            return
        if product_type not in ('physical', 'digital', 'service'):
            self.send_error_json("Loại sản phẩm phải là physical, digital hoặc service")
            return
        try:
            price = float(price)
            if price < 0:
                raise ValueError()
        except ValueError:
            self.send_error_json("Giá sản phẩm phải là số không âm")
            return

        if product_type == 'physical':
            try:
                stock_quantity = int(stock_quantity) if stock_quantity is not None else 0
                if stock_quantity < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self.send_error_json("Sản phẩm vật lý bắt buộc phải có số lượng tồn kho >= 0")
                return
        else:
            stock_quantity = None

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE products SET name = ?, product_type = ?, price = ?, description = ?, stock_quantity = ? WHERE id = ?;",
                (name, product_type, price, description, stock_quantity, item_id)
            )
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
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa sản phẩm thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa sản phẩm: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # CÁC HÀM XỬ LÝ KHÁCH HÀNG (CUSTOMERS)
    # =========================================================================
    def handle_get_customers(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM customers ORDER BY id DESC;").fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_create_customer(self, data):
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip() or None
        zalo = data.get('zalo', '').strip() or None

        if not name:
            self.send_error_json("Tên khách hàng không được để trống")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO customers (name, phone, zalo) VALUES (?, ?, ?);",
                (name, phone, zalo)
            )
            new_id = cur.lastrowid
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã thêm khách hàng thành công", "id": new_id})
        except sqlite3.IntegrityError:
            self.send_error_json("Số điện thoại này đã tồn tại trong danh sách khách hàng")
        except Exception as e:
            self.send_error_json("Lỗi khi thêm khách hàng: " + str(e))
        finally:
            conn.close()

    def handle_update_customer(self, item_id, data):
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip() or None
        zalo = data.get('zalo', '').strip() or None

        if not name:
            self.send_error_json("Tên khách hàng không được để trống")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE customers SET name = ?, phone = ?, zalo = ? WHERE id = ?;",
                (name, phone, zalo, item_id)
            )
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã cập nhật khách hàng thành công"})
        except sqlite3.IntegrityError:
            self.send_error_json("Số điện thoại này đã được dùng cho khách hàng khác")
        except Exception as e:
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
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa khách hàng thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa khách hàng: " + str(e))
        finally:
            conn.close()

    # =========================================================================
    # CÁC HÀM XỬ LÝ ĐƠN HÀNG (ORDERS) & LOGIC TRỪ KHO VẬT LÝ
    # =========================================================================
    def handle_get_orders(self):
        conn = get_db_connection()
        query = """
        SELECT 
            o.id,
            o.customer_id,
            c.name as customer_name,
            c.phone as customer_phone,
            o.product_id,
            p.name as product_name,
            p.product_type,
            p.stock_quantity as remaining_stock,
            o.amount,
            o.status,
            o.purchased_at
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        JOIN products p ON o.product_id = p.id
        ORDER BY o.id DESC;
        """
        rows = conn.execute(query).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        self.send_json({"success": True, "data": data})

    def handle_create_order(self, data):
        """
        LOGIC TRỪ TỒN KHO:
        - Sản phẩm vật lý ('physical') -> Tự động trừ 1 vào stock_quantity.
        - Sản phẩm số ('digital') / dịch vụ ('service') -> Giữ nguyên tồn kho.
        """
        try:
            customer_id = int(data.get('customer_id'))
            product_id = int(data.get('product_id'))
        except (ValueError, TypeError):
            self.send_error_json("Vui lòng chọn khách hàng và sản phẩm hợp lệ")
            return

        status = data.get('status', 'pending')
        if status not in ('pending', 'paid', 'processing', 'completed', 'cancelled'):
            status = 'pending'

        conn = get_db_connection()
        cur = conn.cursor()

        try:
            cust = cur.execute("SELECT id, name FROM customers WHERE id = ?;", (customer_id,)).fetchone()
            if not cust:
                self.send_error_json("Khách hàng không tồn tại")
                return

            prod = cur.execute("SELECT id, name, product_type, price, stock_quantity FROM products WHERE id = ?;", (product_id,)).fetchone()
            if not prod:
                self.send_error_json("Sản phẩm không tồn tại")
                return

            prod_dict = dict(prod)
            p_type = prod_dict['product_type']
            stock = prod_dict['stock_quantity']

            amount = data.get('amount')
            if amount is None or amount == '':
                amount = prod_dict['price']
            else:
                amount = float(amount)
                if amount < 0:
                    raise ValueError()

            stock_deducted = False
            remaining_stock = stock

            if p_type == 'physical':
                if stock is None or stock <= 0:
                    self.send_error_json(f"Sản phẩm vật lý '{prod_dict['name']}' đã hết hàng trong kho (Tồn kho: 0)!")
                    return
                cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (product_id,))
                stock_deducted = True
                remaining_stock = stock - 1
            else:
                stock_deducted = False

            cur.execute(
                "INSERT INTO orders (customer_id, product_id, amount, status) VALUES (?, ?, ?, ?);",
                (customer_id, product_id, amount, status)
            )
            order_id = cur.lastrowid
            conn.commit()
            sync_db_copies()

            message = f"Đã tạo đơn hàng #{order_id} thành công!"
            if stock_deducted:
                message += f" Đã tự động trừ tồn kho (Tồn kho còn lại: {remaining_stock})."
            else:
                message += f" Sản phẩm dạng {p_type.upper()} không trừ tồn kho."

            self.send_json({
                "success": True,
                "message": message,
                "order_id": order_id,
                "stock_deducted": stock_deducted,
                "remaining_stock": remaining_stock
            })

        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi khi tạo đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_update_order(self, item_id, data):
        status = data.get('status')
        amount = data.get('amount')

        if not status or status not in ('pending', 'paid', 'processing', 'completed', 'cancelled'):
            self.send_error_json("Trạng thái đơn hàng không hợp lệ")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            if amount is not None:
                amount = float(amount)
                cur.execute("UPDATE orders SET status = ?, amount = ? WHERE id = ?;", (status, amount, item_id))
            else:
                cur.execute("UPDATE orders SET status = ? WHERE id = ?;", (status, item_id))
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã cập nhật đơn hàng thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi cập nhật đơn hàng: " + str(e))
        finally:
            conn.close()

    def handle_delete_order(self, item_id):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM orders WHERE id = ?;", (item_id,))
            conn.commit()
            sync_db_copies()
            self.send_json({"success": True, "message": "Đã xóa đơn hàng thành công"})
        except Exception as e:
            self.send_error_json("Lỗi khi xóa đơn hàng: " + str(e))
    def handle_public_create_order(self, payload):
        """Khách hàng thanh toán từ cổng /thanhtoan -> Tự động lưu khách hàng, đơn hàng và trừ kho vật lý"""
        cust_name = str(payload.get('customer_name') or 'Khách vãng lai').strip()
        cust_phone = str(payload.get('customer_phone') or '').strip() or None
        prod_id = payload.get('product_id')
        prod_name = str(payload.get('product_name') or '').strip()
        amount = payload.get('amount')
        status = payload.get('status') or 'paid'

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # 1. Tìm hoặc tạo khách hàng
            cust_id = None
            if cust_phone:
                cur.execute("SELECT id FROM customers WHERE phone = ?;", (cust_phone,))
                row = cur.fetchone()
                if row:
                    cust_id = row[0]
            if not cust_id:
                cur.execute("INSERT INTO customers (name, phone, zalo) VALUES (?, ?, ?);",
                            (cust_name, cust_phone, cust_phone))
                cust_id = cur.lastrowid

            # 2. Tìm sản phẩm
            prod = None
            if prod_id:
                cur.execute("SELECT id, name, product_type, price, stock_quantity FROM products WHERE id = ?;", (prod_id,))
                prod = cur.fetchone()
            if not prod and prod_name:
                cur.execute("SELECT id, name, product_type, price, stock_quantity FROM products WHERE name LIKE ?;", (f"%{prod_name[:15]}%",))
                prod = cur.fetchone()
            if not prod:
                cur.execute("SELECT id, name, product_type, price, stock_quantity FROM products LIMIT 1;")
                prod = cur.fetchone()

            p_id, p_name, p_type, p_price, p_stock = prod
            order_amount = amount if amount is not None else p_price

            # 3. QUY TẮC CỐT LÕI: Trừ tồn kho nếu là sản phẩm vật lý (physical)
            stock_deducted = False
            if p_type == 'physical':
                if p_stock is not None and p_stock > 0:
                    cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
                    stock_deducted = True

            # 4. Tạo đơn hàng với trạng thái paid
            cur.execute("INSERT INTO orders (customer_id, product_id, amount, status) VALUES (?, ?, ?, ?);",
                        (cust_id, p_id, order_amount, status))
            order_id = cur.lastrowid
            conn.commit()
            sync_db_copies()

            self.send_json({
                "success": True,
                "message": "Đã ghi nhận đơn hàng thanh toán thành công",
                "order_id": order_id,
                "customer_id": cust_id,
                "status": status,
                "stock_deducted": stock_deducted
            }, 201)
        except Exception as e:
            conn.rollback()
            self.send_error_json("Lỗi tạo đơn thanh toán: " + str(e))
        finally:
            conn.close()

    def handle_sepay_webhook(self, payload):
        """Xử lý webhook biến động số dư SePay (VietinBank SEVQR)"""
        content = str(payload.get('content') or payload.get('description') or '').strip()
        amount = payload.get('transferAmount') or payload.get('amount') or 0

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            import re
            phone_match = re.search(r'0\d{9}', content)
            order_updated = False

            if phone_match:
                phone = phone_match.group(0)
                cur.execute("""
                    SELECT o.id, o.product_id, p.product_type, p.stock_quantity
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.id
                    JOIN products p ON o.product_id = p.id
                    WHERE c.phone = ? AND o.status = 'pending'
                    ORDER BY o.id DESC LIMIT 1;
                """, (phone,))
                pending_order = cur.fetchone()

                if pending_order:
                    o_id, p_id, p_type, p_stock = pending_order
                    cur.execute("UPDATE orders SET status = 'paid' WHERE id = ?;", (o_id,))
                    if p_type == 'physical' and p_stock is not None and p_stock > 0:
                        cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
                    order_updated = True
                else:
                    cur.execute("SELECT id FROM customers WHERE phone = ?;", (phone,))
                    c_row = cur.fetchone()
                    if c_row:
                        c_id = c_row[0]
                    else:
                        cur.execute("INSERT INTO customers (name, phone, zalo) VALUES (?, ?, ?);",
                                    (f"Khách hàng SePay {phone}", phone, phone))
                        c_id = cur.lastrowid
                    
                    cur.execute("SELECT id, product_type, stock_quantity FROM products LIMIT 1;")
                    p_id, p_type, p_stock = cur.fetchone()
                    if p_type == 'physical' and p_stock is not None and p_stock > 0:
                        cur.execute("UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?;", (p_id,))
                    cur.execute("INSERT INTO orders (customer_id, product_id, amount, status) VALUES (?, ?, ?, 'paid');",
                                (c_id, p_id, amount))
                    order_updated = True

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
        revenue = cur.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status IN ('paid', 'completed');").fetchone()[0]
        conn.close()
        self.send_json({
            "success": True,
            "data": {
                "products_count": prods,
                "customers_count": custs,
                "orders_count": orders,
                "revenue": float(revenue)
            }
        })


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
