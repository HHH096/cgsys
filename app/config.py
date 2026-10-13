# -*- coding: utf-8 -*-
"""路径与默认配置。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / 'app'
WEB_DIR = APP_DIR / 'web'
DATA_DIR = ROOT / 'data'
ATTACH_DIR = DATA_DIR / 'attachments'
INVOICE_DIR = DATA_DIR / 'invoices'
MAIL_INBOX_DIR = DATA_DIR / 'mail_inbox'      # 邮箱收下来、还没挂到条目的发票
MAIL_CONFIG = DATA_DIR / 'mail.json'          # 邮箱配置（授权码做混淆，非明文）
EXPORT_DIR = ROOT / '导出'
DB_PATH = DATA_DIR / 'procurement.db'
ROSTER_FILE = ROOT / '组员名单.txt'

# 表格标题（与在线表格 A1 一致）
DEFAULT_TITLE = '2026年智能工程与未来学院方程式赛车竞赛设备与耗材采购表'
# 组别
DEFAULT_GROUP = '电气组'
# 拆分阈值：同一商家本轮合计 >= 该值 -> 进「高于一千」
DEFAULT_SPLIT_THRESHOLD = 1000.0
# 单价超过该值需要标黄并提示「需要合同」
CONTRACT_PRICE_HINT = 1000.0

# 采购轮次状态
STATUS_OPEN = 'open'
STATUS_LOCKED = 'locked'
STATUS_DONE = 'done'

# 下单状态（items.order_status）
ORDER_PENDING = '待下单'
ORDER_ORDERED = '已下单'
ORDER_SHIPPED = '已发货'
ORDER_ARRIVED = '已到货'
ORDER_STATUSES = [ORDER_PENDING, ORDER_ORDERED, ORDER_SHIPPED, ORDER_ARRIVED]

# 发票状态（items.invoice_status）
INV_NONE = '未要'
INV_ASKED = '已要'
INV_RECEIVED = '已收到'
INV_STATUSES = [INV_NONE, INV_ASKED, INV_RECEIVED]

# 允许上传的发票文件后缀
INVOICE_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.xml', '.ofd'}


def ensure_dirs():
    for d in (DATA_DIR, ATTACH_DIR, INVOICE_DIR, MAIL_INBOX_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
