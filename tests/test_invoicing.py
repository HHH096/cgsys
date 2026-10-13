# -*- coding: utf-8 -*-
"""P2 发票归档：命名规则、存盘、去重、增删查。"""
import unittest

from app import config
from app.core import invoicing, repo
from tests.support import TempDataTest

PDF = b'%PDF-1.4 fake invoice body'


class TestNaming(unittest.TestCase):

    def test_amount_text_matches_workspace_style(self):
        self.assertEqual(invoicing.amount_text(112.92), '112.92')
        self.assertEqual(invoicing.amount_text(23.0), '23')
        self.assertEqual(invoicing.amount_text(5.10), '5.1')
        self.assertEqual(invoicing.amount_text(2060), '2060')
        self.assertEqual(invoicing.amount_text(0), '0')
        self.assertEqual(invoicing.amount_text(None), '')
        self.assertEqual(invoicing.amount_text(''), '')

    def test_clean_name_strips_illegal_and_spaces(self):
        self.assertEqual(invoicing.clean_name('PCB物料'), 'PCB物料')
        self.assertEqual(invoicing.clean_name('绝缘 手套'), '绝缘手套')
        self.assertEqual(invoicing.clean_name('a/b\\c:d*e?f"g<h>i|j'), 'abcdefghij')
        self.assertEqual(invoicing.clean_name('  .- 排线 - '), '排线')

    def test_filename_matches_existing_workspace_format(self):
        item = {'name': 'PCB物料', 'paid': 112.92}
        self.assertEqual(invoicing.invoice_filename(item, '.pdf'), 'DQ-PCB物料-112.92.pdf')

    def test_paid_wins_over_total(self):
        item = {'name': '防水插头', 'paid': 105.8, 'total': 100.8}
        self.assertEqual(invoicing.invoice_filename(item, '.pdf'), 'DQ-防水插头-105.8.pdf')

    def test_falls_back_to_total(self):
        item = {'name': '螺丝刀', 'paid': None, 'total': 16}
        self.assertEqual(invoicing.invoice_filename(item, '.pdf'), 'DQ-螺丝刀-16.pdf')

    def test_no_amount_means_two_parts(self):
        item = {'name': '绝缘工具', 'paid': None, 'total': None}
        self.assertEqual(invoicing.invoice_filename(item, '.pdf'), 'DQ-绝缘工具.pdf')

    def test_unnamed_item_falls_back(self):
        self.assertEqual(invoicing.invoice_filename({}, '.pdf'), 'DQ-发票.pdf')

    def test_extension_is_normalised(self):
        self.assertEqual(invoicing.invoice_filename({'name': '排线', 'paid': 5.1}, 'PDF'),
                         'DQ-排线-5.1.pdf')
        self.assertEqual(invoicing.invoice_filename({'name': '排线', 'paid': 5.1}, '.PNG'),
                         'DQ-排线-5.1.png')


class TestStorage(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '13800000000')
        self.rid = repo.create_round('八月采购')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': 'PCB物料', 'qty': 1, 'unit': '套', 'price': 158,
            'purpose': 'tsal板', 'applicant': '张三', 'paid': 157.92,
        })
        self.item = repo.get_item(self.iid)

    def store(self, data=PDF, name='invoice.pdf'):
        return invoicing.store_invoice(self.item, '八月采购', name, data)

    def test_stored_under_round_folder_with_rule_name(self):
        rec = self.store()
        self.assertEqual(rec['stored_name'], 'DQ-PCB物料-157.92.pdf')
        self.assertEqual(rec['state'], 'new')
        self.assertEqual(rec['rel_path'], 'invoices/八月采购/DQ-PCB物料-157.92.pdf')
        self.assertTrue(rec['abs_path'].exists())
        self.assertEqual(rec['abs_path'].read_bytes(), PDF)

    def test_rel_path_is_under_data_dir(self):
        rec = self.store()
        full = config.DATA_DIR / rec['rel_path']
        self.assertTrue(full.exists())

    def test_same_file_uploaded_twice_is_not_duplicated(self):
        first = self.store()
        second = self.store()
        self.assertEqual(second['state'], 'duplicate')
        self.assertEqual(second['stored_name'], first['stored_name'])
        files = list((config.INVOICE_DIR / '八月采购').iterdir())
        self.assertEqual(len(files), 1, '重复上传不该产生副本')

    def test_different_file_with_same_name_gets_suffix(self):
        first = self.store()
        second = self.store(data=PDF + b'x')
        self.assertEqual(second['state'], 'new')
        self.assertEqual(second['stored_name'], 'DQ-PCB物料-157.92-2.pdf')
        self.assertNotEqual(first['stored_name'], second['stored_name'])

    def test_unsupported_extension_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.store(name='发票.exe')
        self.assertIn('不支持', str(cm.exception))

    def test_empty_file_rejected(self):
        with self.assertRaises(ValueError):
            self.store(data=b'')

    def test_xml_and_ofd_are_stored_as_they_are(self):
        """XML / OFD 不是图片，原样存 —— 没法把它们转成 PDF。"""
        for name, data in (('a.xml', b'<xml/>'), ('a.ofd', b'ofd')):
            rec = invoicing.store_invoice(self.item, '八月采购', name, data)
            self.assertTrue(rec['stored_name'].startswith('DQ-PCB物料-157.92'))
            self.assertFalse(rec['converted'])

    def test_a_fake_image_is_rejected(self):
        """光扩展名对已经不够了：图片要**真能打开**才收（归档时要转成 PDF）。

        真图片转 PDF 的那一套在 `tests/test_image_invoice.py`。
        """
        with self.assertRaises(ValueError):
            self.store(name='假的.png', data=b'\x89PNG')

    def test_delete_file(self):
        rec = self.store()
        self.assertTrue(rec['abs_path'].exists())
        invoicing.delete_file(rec['rel_path'])
        self.assertFalse(rec['abs_path'].exists())
        invoicing.delete_file(rec['rel_path'])      # 再删一次不报错
        invoicing.delete_file('')                   # 空路径也不报错


class TestInvoiceRecords(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三')
        self.rid = repo.create_round('八月采购')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': 'PCB物料', 'qty': 1, 'unit': '套', 'price': 158,
            'purpose': 'tsal板', 'applicant': '张三', 'paid': 157.92})
        self.item = repo.get_item(self.iid)

    def attach(self, name='invoice.pdf', data=PDF):
        rec = invoicing.store_invoice(self.item, '八月采购', name, data)
        inv_id = repo.add_invoice(self.iid, self.rid, rec['orig_name'], rec['stored_name'],
                                  rec['rel_path'], rec['size'], amount=157.92)
        return inv_id, rec

    def test_attach_and_list(self):
        inv_id, rec = self.attach()
        got = repo.list_invoices(item_id=self.iid)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['id'], inv_id)
        self.assertEqual(got[0]['stored_name'], 'DQ-PCB物料-157.92.pdf')
        self.assertEqual(got[0]['amount'], 157.92)
        self.assertTrue(got[0]['created_at'])

    def test_list_by_round(self):
        self.attach()
        self.assertEqual(len(repo.list_invoices(round_id=self.rid)), 1)
        self.assertEqual(repo.list_invoices(round_id=999), [])

    def test_counts_per_item(self):
        self.attach()
        self.attach(name='second.pdf', data=PDF + b'y')
        self.assertEqual(repo.invoice_counts(self.rid), {self.iid: 2})

    def test_delete_removes_record_and_file(self):
        inv_id, rec = self.attach()
        self.assertTrue(rec['abs_path'].exists())
        self.assertTrue(repo.delete_invoice(inv_id))
        self.assertEqual(repo.list_invoices(item_id=self.iid), [])
        self.assertFalse(rec['abs_path'].exists(), '删记录时应连文件一起删')

    def test_delete_missing_returns_false(self):
        self.assertFalse(repo.delete_invoice(12345))

    def test_deleting_item_cascades_to_invoices(self):
        self.attach()
        repo.delete_item(self.iid)
        self.assertEqual(repo.list_invoices(round_id=self.rid), [])

    def test_deleting_the_last_invoice_resets_the_item_status(self):
        """删掉最后一张发票后必须退回「未要」，不能显示已收到却没有发票。"""
        inv_id, _ = self.attach()
        repo.set_item_status(self.iid, invoice_status='已收到')
        repo.delete_invoice(inv_id)
        it = repo.get_item(self.iid)
        self.assertEqual(it['invoice_status'], '未要')
        self.assertEqual(it['invoice'], '', '「发票」列也要跟着退回')

    def test_deleting_one_of_two_invoices_keeps_the_status(self):
        repo.set_item_status(self.iid, invoice_status='已收到')
        first, _ = self.attach()
        second, _ = self.attach(name='second.pdf', data=PDF + b'y')
        repo.delete_invoice(first)
        self.assertEqual(repo.get_item(self.iid)['invoice_status'], '已收到')
        repo.delete_invoice(second)
        self.assertEqual(repo.get_item(self.iid)['invoice_status'], '未要')


if __name__ == '__main__':
    unittest.main(verbosity=2)
