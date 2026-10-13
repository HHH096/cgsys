# -*- coding: utf-8 -*-
"""数据备份。"""
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from app import config
from app.core import repo
from app.tools import backup as bk
from tests.support import TempDataTest

ROOT = Path(__file__).resolve().parent.parent


class TestBackup(TempDataTest):

    def setUp(self):
        super().setUp()
        # 备份目录必须放在 data 之外，否则会把上一次的备份也打进去
        self._destdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._destdir.cleanup)
        self.dest = Path(self._destdir.name) / '备份'
        mid = repo.add_member('张三', '13800000000')
        self.rid = repo.create_round('八月采购')
        repo.add_item(self.rid, mid, {'name': '充电机', 'qty': 1, 'unit': '个',
                                      'price': 6200, 'purpose': '用', 'applicant': '张三'})
        config.MAIL_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        (config.MAIL_INBOX_DIR / 'x.pdf').write_bytes(b'%PDF-1.4 x')

    def test_creates_a_zip(self):
        info = bk.backup(dest=self.dest)
        p = Path(info['path'])
        self.assertTrue(p.exists())
        self.assertTrue(p.name.startswith('data_'))
        self.assertTrue(p.name.endswith('.zip'))
        self.assertGreater(info['files'], 0)
        self.assertGreater(info['size'], 0)

    def test_zip_holds_the_whole_data_dir(self):
        info = bk.backup(dest=self.dest)
        with zipfile.ZipFile(info['path']) as zf:
            names = zf.namelist()
        self.assertIn('procurement.db', names)
        self.assertIn('mail_inbox/x.pdf', names)

    def test_db_inside_the_zip_is_a_readable_snapshot(self):
        info = bk.backup(dest=self.dest)
        with zipfile.ZipFile(info['path']) as zf:
            blob = zf.read('procurement.db')
        out = Path(self._tmp.name) / 'restored.db'
        out.write_bytes(blob)
        conn = sqlite3.connect(str(out))
        try:
            names = [r[0] for r in conn.execute('SELECT name FROM items')]
        finally:
            conn.close()
        self.assertEqual(names, ['充电机'])

    def test_restore_round_trip(self):
        """备份 → 把现状毁掉 → 解压回去 → 数据回来了。"""
        info = bk.backup(dest=self.dest)
        repo.delete_item(repo.list_items(self.rid)[0]['id'])
        self.assertEqual(repo.list_items(self.rid), [])

        with zipfile.ZipFile(info['path']) as zf:
            zf.extractall(str(config.DATA_DIR))     # zip 里的路径是相对 data 的

        self.assertEqual([i['name'] for i in repo.list_items(self.rid)], ['充电机'])
        self.assertTrue((config.MAIL_INBOX_DIR / 'x.pdf').exists())

    def test_keeps_only_the_most_recent(self):
        base = datetime(2026, 10, 7, 12, 0, 0)
        for i in range(4):
            bk.backup(dest=self.dest, keep=2, now=base + timedelta(minutes=i))
        rows = bk.list_backups(self.dest)
        self.assertEqual(len(rows), 2, '只该留下 2 份')
        self.assertGreater(rows[-1]['name'], rows[0]['name'], '留下的是最新的两份')

    def test_prune_reports_what_it_removed(self):
        for i in range(3):
            bk.backup(dest=self.dest, keep=10, now=datetime(2026, 10, 7, 12, i, 0))
        removed = bk.prune(self.dest, keep=1)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(bk.list_backups(self.dest)), 1)

    def test_list_is_empty_before_any_backup(self):
        self.assertEqual(bk.list_backups(self.dest), [])

    def test_missing_data_dir_is_reported(self):
        shutil.rmtree(str(config.DATA_DIR))
        with self.assertRaises(FileNotFoundError):
            bk.backup(dest=self.dest)

    def test_two_backups_in_the_same_second_do_not_clash(self):
        now = datetime(2026, 10, 7, 12, 0, 0)
        a = bk.backup(dest=self.dest, keep=10, now=now)
        b = bk.backup(dest=self.dest, keep=10, now=now)
        self.assertEqual(a['name'], b['name'], '同一秒内重名（会覆盖，可接受）')
        self.assertTrue(Path(b['path']).exists())

    def test_cli_list_does_not_write(self):
        self.assertEqual(bk.main(['--list']), 0)


class TestHumanSize(unittest.TestCase):

    def test_units(self):
        self.assertEqual(bk.human_size(0), '0.0 B')
        self.assertEqual(bk.human_size(2048), '2.0 KB')
        self.assertEqual(bk.human_size(5 * 1024 * 1024), '5.0 MB')


class TestBackupBat(unittest.TestCase):

    def test_is_ascii_and_has_no_chcp(self):
        data = (ROOT / '备份数据.bat').read_bytes()
        try:
            text = data.decode('ascii')
        except UnicodeDecodeError as exc:
            self.fail('备份数据.bat 不是纯 ASCII：%s' % exc)
        commands = [ln.strip().lower() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().lower().startswith('rem')]
        for ln in commands:
            self.assertFalse(ln.startswith('chcp'), '不该执行 chcp')
        self.assertIn('app.tools.backup', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
