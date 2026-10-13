# -*- coding: utf-8 -*-
"""SQLite 连接与表结构。"""
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL UNIQUE,
    phone    TEXT    NOT NULL DEFAULT '',
    is_owner INTEGER NOT NULL DEFAULT 0,
    sort     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    group_name      TEXT    NOT NULL DEFAULT '电气组',
    status          TEXT    NOT NULL DEFAULT 'open',
    split_threshold REAL    NOT NULL DEFAULT 1000,
    created_at      TEXT    NOT NULL,
    locked_at       TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id       INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    member_id      INTEGER REFERENCES members(id) ON DELETE SET NULL,
    seq            INTEGER NOT NULL DEFAULT 0,
    name           TEXT NOT NULL DEFAULT '',
    spec           TEXT NOT NULL DEFAULT '',
    qty            REAL,
    unit           TEXT NOT NULL DEFAULT '',
    price          REAL,
    subtotal       REAL,
    freight        REAL NOT NULL DEFAULT 0,
    total          REAL,
    stock          TEXT NOT NULL DEFAULT '',
    purpose        TEXT NOT NULL DEFAULT '',
    applicant      TEXT NOT NULL DEFAULT '',
    payer          TEXT NOT NULL DEFAULT '',
    used_at        TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    image_path     TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    group_name     TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    paid           REAL,
    invoice        TEXT NOT NULL DEFAULT '',
    arrived        TEXT NOT NULL DEFAULT '',
    has_contract   TEXT NOT NULL DEFAULT '',
    split_override TEXT NOT NULL DEFAULT '',
    order_status   TEXT NOT NULL DEFAULT '待下单',
    invoice_status TEXT NOT NULL DEFAULT '未要',
    refunded       INTEGER NOT NULL DEFAULT 0,
    refund_amount  REAL NOT NULL DEFAULT 0,
    rev            INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_round  ON items(round_id);
CREATE INDEX IF NOT EXISTS idx_items_member ON items(member_id);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    round_id     INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    orig_name    TEXT    NOT NULL DEFAULT '',
    stored_name  TEXT    NOT NULL DEFAULT '',
    rel_path     TEXT    NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    amount       REAL,
    invoice_no   TEXT    NOT NULL DEFAULT '',
    invoice_date TEXT    NOT NULL DEFAULT '',
    seller       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoices_item  ON invoices(item_id);
CREATE INDEX IF NOT EXISTS idx_invoices_round ON invoices(round_id);

CREATE TABLE IF NOT EXISTS mail_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_uid    TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL DEFAULT '',
    sender      TEXT    NOT NULL DEFAULT '',
    mail_date   TEXT    NOT NULL DEFAULT '',
    orig_name   TEXT    NOT NULL DEFAULT '',
    stored_name TEXT    NOT NULL DEFAULT '',
    rel_path    TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    sha1        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending',
    item_id     INTEGER REFERENCES items(id) ON DELETE SET NULL,
    invoice_id  INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_inbox_status ON mail_inbox(status);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_sha1   ON mail_inbox(sha1);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_uid    ON mail_inbox(mail_uid);

-- 发票 ↔ 条目 的多对多关系。
--
-- 淘宝一个订单买好几样（电容 + 电阻 + 芯片），商家只开一张发票 ——
-- 所以「这张票归哪条采购」**不能**写死在 invoices.item_id 上，
-- 否则另外几条永远显示「未要」，缺发票清单永远清不掉；
-- 而如果每条都复制一份发票，合并打印又会把同一张票印好几遍（财务会当成重复报销）。
--
-- 因此：一条归属一行。invoices.item_id 保留作「主条目」（老数据一点不动），
-- 新逻辑一律以这张表为准。
CREATE TABLE IF NOT EXISTS invoice_items (
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id)    ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (invoice_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_inv_items_item    ON invoice_items(item_id);
CREATE INDEX IF NOT EXISTS idx_inv_items_invoice ON invoice_items(invoice_id);
"""

# 老库升级：给已存在的 items 表补字段（新库由 CREATE TABLE 直接带上）
MIGRATIONS = [
    ('items', 'order_status',
     "ALTER TABLE items ADD COLUMN order_status TEXT NOT NULL DEFAULT '待下单'"),
    ('items', 'invoice_status',
     "ALTER TABLE items ADD COLUMN invoice_status TEXT NOT NULL DEFAULT '未要'"),
    # 整单退货：refunded=1 表示这单退了，refund_amount 是退回来的钱
    # （默认按实付全退，运费不退之类的可以填小一点）
    ('items', 'refunded',
     'ALTER TABLE items ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0'),
    ('items', 'refund_amount',
     'ALTER TABLE items ADD COLUMN refund_amount REAL NOT NULL DEFAULT 0'),
    # 垫资人：组员自己先掏钱买的时候记一下（空 = 不是个人垫的）。
    # 只在本系统里记录，**不进车队总表**（那边列是固定的）。
    ('items', 'payer',
     "ALTER TABLE items ADD COLUMN payer TEXT NOT NULL DEFAULT ''"),
    # 内容版本号：**有人改过这行内容**就 +1。
    # 多人同时在线时，谁的表单是照着旧版本填的、保存会被拦下来（见 api_update_item），
    # 免得后按保存的人一声不响地把别人刚填的覆盖掉。
    # 注意：只改「下单/发票状态」**不算**内容变更 —— 采购边下单、组员边补资料
    # 是很正常的事，那种不该互相拦。
    ('items', 'rev',
     'ALTER TABLE items ADD COLUMN rev INTEGER NOT NULL DEFAULT 0'),
]


def backfill_invoice_items(conn):
    """把老数据「一张票一个条目」灌进关联表。

    **幂等**：`INSERT OR IGNORE` + 主键 (invoice_id, item_id)，
    跑多少遍都不会重复。返回这次实际插了几行。
    """
    cur = conn.execute(
        'INSERT OR IGNORE INTO invoice_items (invoice_id, item_id, created_at) '
        'SELECT id, item_id, created_at FROM invoices WHERE item_id IS NOT NULL'
    )
    return cur.rowcount


def migrate(conn):
    """按需补列 + 回填关联表。返回做了什么。"""
    added = []
    for table, column, sql in MIGRATIONS:
        cols = {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}
        if column not in cols:
            conn.execute(sql)
            added.append('%s.%s' % (table, column))
    moved = backfill_invoice_items(conn)
    if moved:
        added.append('invoice_items +%d' % moved)
    return added


def connect():
    """开一个连接。每次调用都新建，SQLite 在局域网小规模使用下足够。

    **多人同时用**是这个系统的常态（组员一起填、采购同时在下单核发票），
    所以写锁要等得起：
    * `timeout` / `busy_timeout` 都是 15 秒 —— 默认 5 秒在「自动收信线程
      正好在写库」的时候会偶发 `database is locked`，对用户就是一条 500。
      这里的写事务都是毫秒级的，等一下就轮到，不该直接失败。
    * 不开 WAL：备份脚本是**整个文件复制**的，WAL 模式下那些还在 `-wal`
      里没落盘的数据会漏备，得不偿失。
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 15000')
    return conn


@contextmanager
def session():
    """事务上下文：正常提交，异常回滚，最后一定关闭连接。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    config.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()
