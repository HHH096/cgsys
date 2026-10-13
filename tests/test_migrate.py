# -*- coding: utf-8 -*-
"""P2 数据层：新增字段、invoices 表、老库迁移。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config
from app.core import db

# 老结构的 items（没有 order_status / invoice_status）
OLD_ITEMS = """
CREATE TABLE items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id       INTEGER NOT NULL DEFAULT 0,
    member_id      INTEGER,
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
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""


class MigrateCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name, val in (('DATA_DIR', self.root),
                          ('ATTACH_DIR', self.root / 'attachments'),
                          ('INVOICE_DIR', self.root / 'invoices'),
                          ('EXPORT_DIR', self.root / 'export'),
                          ('DB_PATH', self.root / 'procurement.db')):
            p = mock.patch.object(config, name, val)
            p.start()
            self.addCleanup(p.stop)

    def schema(self):
        conn = db.connect()
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(items)')}
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            inv_cols = {r[1] for r in conn.execute('PRAGMA table_info(invoices)')}
            return cols, tables, inv_cols
        finally:
            conn.close()


class TestFreshDatabase(MigrateCase):

    def test_new_columns_exist(self):
        db.init_db()
        cols, _, _ = self.schema()
        self.assertIn('order_status', cols)
        self.assertIn('invoice_status', cols)

    def test_invoices_table_exists(self):
        db.init_db()
        _, tables, inv_cols = self.schema()
        self.assertIn('invoices', tables)
        for c in ('item_id', 'round_id', 'orig_name', 'stored_name', 'rel_path',
                  'size', 'amount', 'invoice_no', 'invoice_date', 'seller', 'created_at'):
            self.assertIn(c, inv_cols, 'invoices 缺列 %s' % c)

    def test_defaults(self):
        db.init_db()
        conn = db.connect()
        try:
            conn.execute("INSERT INTO rounds(name, title, group_name, status, split_threshold,"
                         " created_at) VALUES ('八月采购','t','电气组','open',1000,'now')")
            conn.execute("INSERT INTO items(round_id, created_at, updated_at)"
                         " VALUES (1,'now','now')")
            conn.commit()
            row = conn.execute('SELECT order_status, invoice_status FROM items').fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ('待下单', '未要'))


class TestOldDatabaseUpgrade(MigrateCase):

    def make_old_db(self):
        conn = sqlite3.connect(str(config.DB_PATH))
        try:
            conn.executescript(OLD_ITEMS)
            conn.execute("INSERT INTO items(round_id, name, qty, price, subtotal, total,"
                         " created_at, updated_at) VALUES (1,'旧数据',2,10,20,20,'x','x')")
            conn.commit()
        finally:
            conn.close()

    def test_old_columns_are_added(self):
        self.make_old_db()
        cols, _, _ = self.schema()
        self.assertNotIn('order_status', cols, '前置条件：老库不该有该列')

        db.init_db()

        cols, tables, _ = self.schema()
        self.assertIn('order_status', cols)
        self.assertIn('invoice_status', cols)
        self.assertIn('invoices', tables, '老库也要补出 invoices 表')

    def test_existing_rows_survive_with_defaults(self):
        self.make_old_db()
        db.init_db()
        conn = db.connect()
        try:
            row = conn.execute('SELECT name, subtotal, order_status, invoice_status FROM items').fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], '旧数据')
        self.assertEqual(row[1], 20)
        self.assertEqual(row[2], '待下单')
        self.assertEqual(row[3], '未要')

    def test_migrate_is_idempotent(self):
        db.init_db()
        conn = db.connect()
        try:
            self.assertEqual(db.migrate(conn), [], '第二次迁移不该再补任何列')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
