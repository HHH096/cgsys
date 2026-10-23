# -*- coding: utf-8 -*-
"""数据自检工具：报告「库里有记录 / 磁盘没文件」这类对不上的情况。"""
from app import config
from app.core import db, repo
from app.tools import doctor
from tests.support import TempDataTest


class DoctorCase(TempDataTest):
    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.rid = repo.create_round('九月采购')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': '电容', 'qty': 1, 'unit': '个', 'price': 10,
            'purpose': 'x', 'applicant': '张三'})

    def add_invoice(self, name='票.pdf', make_file=True):
        rel = 'invoices/' + name
        p = config.DATA_DIR / rel
        if make_file:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'%PDF-1.4')
        return repo.add_invoice(self.iid, self.rid, name, name, rel, 9, amount=10.0)

    def force_status(self, value):
        """绕过接口直接改库 —— 只有手工改库/恢复备份才会造成这种不一致。"""
        with db.session() as c:
            c.execute('UPDATE items SET invoice_status = ? WHERE id = ?',
                      (value, self.iid))

    def kinds(self):
        return [k for _, k, _ in doctor.check()]


class TestAConsistentDatabase(DoctorCase):
    def test_nothing_is_reported(self):
        self.add_invoice()
        self.assertEqual(doctor.check(), [])

    def test_the_exit_code_is_zero(self):
        self.add_invoice()
        self.assertEqual(doctor.main([]), 0)


class TestMissingFiles(DoctorCase):
    def test_a_record_without_its_file(self):
        self.add_invoice(make_file=False)
        self.assertIn('发票文件丢了', self.kinds())

    def test_the_exit_code_is_nonzero(self):
        self.add_invoice(make_file=False)
        self.assertEqual(doctor.main([]), 1)


class TestFilesNobodyOwns(DoctorCase):
    def test_a_file_without_a_record_is_just_a_warning(self):
        p = config.INVOICE_DIR / '九月采购' / '野的.pdf'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'%PDF-1.4')
        got = doctor.check()
        self.assertIn('多余的文件', [k for _, k, _ in got])
        self.assertEqual([lv for lv, k, _ in got if k == '多余的文件'], ['警告'])


class TestStatusDrift(DoctorCase):
    """`invoice_status` 只能由「传发票 / 撤发票」驱动，正常流程不会错位。"""

    def test_an_invoice_but_the_flag_is_stuck(self):
        self.add_invoice()
        self.force_status('未要')
        self.assertIn('有票但状态没更新', self.kinds())

    def test_a_flag_without_any_invoice(self):
        self.force_status(config.INV_RECEIVED)
        self.assertIn('状态说有票但实际没挂上', self.kinds())

    def test_a_refunded_item_is_not_nagged_about(self):
        """退货时票跟着删了，不该再报「状态说有票」。"""
        self.force_status(config.INV_RECEIVED)
        repo.set_refund(self.iid)
        self.assertNotIn('状态说有票但实际没挂上', self.kinds())


class TestAttachedRecordsAreNotFalseAlarms(DoctorCase):
    """已挂的票，文件已经归档进 `invoices/` 了。

    `mail_inbox.rel_path` 留的是**移走之前**的旧路径。按「文件丢了」报是误报 ——
    线上第一次跑这个工具时报了 20 条，全部是这种，逐条核对后文件都在 `invoices/` 里。
    """

    def _mail(self, rel_path, invoice_id=None, status='attached'):
        with db.session() as c:
            c.execute(
                'INSERT INTO mail_inbox (mail_uid, rel_path, orig_name, stored_name, '
                'status, invoice_id, item_id, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                ('uid-' + rel_path, rel_path, 'x.pdf', 'x.pdf',
                 status, invoice_id, self.iid, '2026-01-01 00:00:00'))

    def test_a_stale_path_on_an_attached_record_is_fine(self):
        inv = self.add_invoice()
        self._mail('mail_inbox/gone.pdf', inv)
        self.assertEqual(doctor.check(), [], '不该报文件丢了')

    def test_an_attached_record_without_an_invoice_is_reported(self):
        """真实场景是「删发票 → 外键 SET NULL」，记录留着但 invoice_id 空了。"""
        self._mail('mail_inbox/y.pdf', None)
        self.assertIn('已挂记录指向不存在的发票', self.kinds())

    def test_a_pending_record_with_a_missing_file_is_still_reported(self):
        self._mail('mail_inbox/p.pdf', None, status='pending')
        self.assertIn('待归档文件丢了', self.kinds())
