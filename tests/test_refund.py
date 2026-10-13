# -*- coding: utf-8 -*-
"""退货：整单退、金额按净额、发票一起删。

按定下来的四条规则：
  1. 标记「已退货」保留在列表里
  2. 只支持整单退
  3. 导出按净额（全退就是 0）
  4. 退货时把发票一起删掉（电子发票也删）
"""
import unittest
from pathlib import Path

from app import config
from app.core import repo, rules
from tests.support import EntryCase, TEST_PDF as PDF

ROOT = Path(__file__).resolve().parent.parent


class TestRefundData(EntryCase):
    """数据层的规则。界面与导出在别的测试里管。"""

    def _item(self, mid=None, paid=100):
        iid = self._add(self.purchase, mid if mid is not None else self.alice, '要退的东西')
        repo.update_item(iid, {'paid': paid, 'price': paid, 'qty': 1})
        return iid

    # ---------------------------------------------------------- 字段

    def test_a_new_item_is_not_refunded(self):
        it = repo.get_item(self._item())
        self.assertEqual(it['refunded'], 0)
        self.assertEqual(it['refund_amount'], 0)

    def test_the_migration_adds_the_columns(self):
        """老库升级路径：migrate 得把这两列补上。"""
        from app.core import db
        with db.session() as c:
            cols = {row[1] for row in c.execute('PRAGMA table_info(items)')}
        self.assertIn('refunded', cols)
        self.assertIn('refund_amount', cols)

    # ---------------------------------------------------------- 净额

    def test_refund_defaults_to_the_whole_paid_amount(self):
        """不指定金额就是「全退」—— 实付多少退多少。"""
        iid = self._item(paid=88.5)
        it = repo.set_refund(iid)
        self.assertEqual(it['refunded'], 1)
        self.assertEqual(it['refund_amount'], 88.5)

    def test_net_paid_is_zero_after_a_full_refund(self):
        iid = self._item(paid=88.5)
        repo.set_refund(iid)
        self.assertEqual(repo.item_net_paid(repo.get_item(iid)), 0)

    def test_a_smaller_refund_leaves_a_net_amount(self):
        """「全退就是 0」的另一面：运费不退时可以填小一点。"""
        iid = self._item(paid=100)
        repo.set_refund(iid, refund_amount=90)
        it = repo.get_item(iid)
        self.assertEqual(it['refund_amount'], 90)
        self.assertEqual(repo.item_net_paid(it), 10)

    def test_net_paid_without_refund_is_just_paid(self):
        it = repo.get_item(self._item(paid=42))
        self.assertEqual(repo.item_net_paid(it), 42)

    def test_net_paid_is_zero_when_nothing_was_paid(self):
        iid = self._item(paid=0)
        self.assertEqual(repo.item_net_paid(repo.get_item(iid)), 0)

    def test_a_negative_refund_is_clamped_to_zero(self):
        iid = self._item(paid=100)
        repo.set_refund(iid, refund_amount=-50)
        self.assertEqual(repo.get_item(iid)['refund_amount'], 0)

    # ---------------------------------------------------------- 取消退货

    def test_cancelling_a_refund_restores_everything(self):
        iid = self._item(paid=100)
        repo.set_refund(iid)
        it = repo.set_refund(iid, refunded=False)
        self.assertEqual(it['refunded'], 0)
        self.assertEqual(it['refund_amount'], 0)
        self.assertEqual(repo.item_net_paid(it), 100)

    def test_refunding_a_missing_item(self):
        with self.assertRaises(ValueError):
            repo.set_refund(99999)

    # ---------------------------------------------------------- 发票一起删

    def test_refund_deletes_the_invoices_and_resets_the_status(self):
        iid = self._add(self.purchase, self.alice, '带发票的')
        r = self.purchase.post('/api/items/%d/invoices' % iid,
                               files={'file': ('a.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(repo.list_invoices(item_id=iid)), 1)
        self.assertEqual(repo.get_item(iid)['invoice_status'], config.INV_RECEIVED)

        repo.set_refund(iid)

        self.assertEqual(repo.list_invoices(item_id=iid), [],
                         '退货要把发票一起删掉（用户要求）')
        self.assertEqual(repo.get_item(iid)['invoice_status'], config.INV_NONE)

    def test_refund_leaves_other_items_invoices_alone(self):
        a = self._item(self.alice)
        b = self._item(self.bob)
        for iid in (a, b):
            self.purchase.post('/api/items/%d/invoices' % iid,
                               files={'file': ('x.pdf', PDF, 'application/pdf')})
        repo.set_refund(a)
        self.assertEqual(len(repo.list_invoices(item_id=b)), 1, '别人的发票不能碰')

    # ---------------------------------------------------------- 清单排除

    def test_pending_order_excludes_refunded(self):
        keep = self._item(self.bob)
        gone = self._item(self.alice)
        self.assertIn(gone, [i['id'] for i in repo.items_pending_order(self.rid)])

        repo.set_refund(gone)

        ids = [i['id'] for i in repo.items_pending_order(self.rid)]
        self.assertIn(keep, ids)
        self.assertNotIn(gone, ids)

    def test_pending_invoice_excludes_refunded(self):
        """最要紧的一条：退了的不再算「缺发票」，否则那份清单永远清不干净。"""
        keep = self._item(self.bob)
        gone = self._item(self.alice)

        repo.set_refund(gone)

        ids = [i['id'] for i in repo.items_pending_invoice(self.rid)]
        self.assertIn(keep, ids)
        self.assertNotIn(gone, ids)

    def test_refunded_stays_in_the_item_list(self):
        """规则 1：保留在列表里，不是删掉。"""
        iid = self._item()
        repo.set_refund(iid)
        self.assertIn(iid, [i['id'] for i in repo.list_items(self.rid)])


class TestRefundExport(EntryCase):
    """导出与对账：金额按净额、备注标「已退货」。"""

    def _refunded(self, paid=100, note=''):
        iid = self._add(self.purchase, self.alice, '退货的东西')
        repo.update_item(iid, {'paid': paid, 'price': paid, 'qty': 1, 'note': note})
        repo.set_refund(iid)
        return repo.get_item(iid)

    def test_export_value_gives_the_net_amount(self):
        it = self._refunded(paid=88.5)
        self.assertEqual(rules.export_value(it, 'paid'), 0, '全退就是 0')
        self.assertEqual(it['paid'], 88.5, '原始实付要留着，不是把它改掉')

    def test_export_value_keeps_the_original_note_and_marks_it(self):
        it = self._refunded(note='本来要用来测绝缘的')
        self.assertEqual(rules.export_value(it, 'note'), '本来要用来测绝缘的（已退货）')

    def test_export_value_marks_an_empty_note(self):
        it = self._refunded(note='')
        self.assertEqual(rules.export_value(it, 'note'), '已退货')

    def test_an_untouched_item_is_left_alone(self):
        iid = self._add(self.purchase, self.alice, '没退的')
        repo.update_item(iid, {'paid': 50, 'price': 50, 'qty': 1, 'note': '备注'})
        it = repo.get_item(iid)
        self.assertEqual(rules.export_value(it, 'paid'), 50)
        self.assertEqual(rules.export_value(it, 'note'), '备注')

    def test_the_block_row_uses_the_net_amount(self):
        from app.core import blockexport
        row = blockexport.block_rows([self._refunded(paid=100)])[0]
        self.assertEqual(row[blockexport.FIELD_ORDER.index('paid')], 0)
        self.assertEqual(row[blockexport.FIELD_ORDER.index('note')], '已退货')

    def test_the_block_tsv_uses_the_net_amount(self):
        from app.core import blockexport
        tsv = blockexport.block_tsv([self._refunded(paid=100)])
        cells = tsv.split('\t')
        self.assertEqual(cells[blockexport.FIELD_ORDER.index('paid')], '0')
        self.assertEqual(cells[blockexport.FIELD_ORDER.index('note')], '已退货')

    def test_the_exported_sheet_uses_the_net_amount(self):
        from app.core import exporter
        it = self._refunded(paid=200)
        wb, _ = exporter.build_workbook(repo.get_round(self.rid), [it])
        ws = wb[exporter.SHEET_MAIN]
        fields = [c[0] for c in exporter.MAIN_COLUMNS]
        self.assertEqual(ws.cell(row=3, column=fields.index('paid') + 1).value, 0)
        self.assertEqual(ws.cell(row=3, column=fields.index('note') + 1).value, '已退货')

    def test_reconcile_separates_refund_and_net(self):
        from app.core import blockexport
        a = self._add(self.purchase, self.alice, '要退的')
        repo.update_item(a, {'paid': 300, 'price': 300, 'qty': 1})
        b = self._add(self.purchase, self.bob, '不退了')
        repo.update_item(b, {'paid': 100, 'price': 100, 'qty': 1})
        repo.set_refund(a)

        r = blockexport.reconcile(repo.list_items(self.rid))
        self.assertEqual(r['paid'], 400, '实付合计还是 400')
        self.assertEqual(r['refund'], 300)
        self.assertEqual(r['net'], 100, '净支出 = 实付 − 退款')
        self.assertEqual(r['refunded_count'], 1)

    def test_reconcile_without_refunds_is_unchanged(self):
        """没有退货时，净支出就等于实付 —— 老行为不能被改坏。"""
        from app.core import blockexport
        a = self._add(self.purchase, self.alice)
        repo.update_item(a, {'paid': 100, 'price': 100, 'qty': 1})
        r = blockexport.reconcile(repo.list_items(self.rid))
        self.assertEqual(r['refund'], 0)
        self.assertEqual(r['net'], r['paid'])
        self.assertEqual(r['refunded_count'], 0)


class TestRefundApi(EntryCase):
    """接口层：退货是采购人员的专属操作。"""

    def _item(self, paid=100):
        iid = self._add(self.purchase, self.alice, '要退的')
        repo.update_item(iid, {'paid': paid, 'price': paid, 'qty': 1})
        return iid

    def test_refund_needs_purchase_login(self):
        iid = self._item()
        for label, client in (('匿名', self.anon), ('组员', self.alice_c)):
            r = client.post('/api/items/%d/refund' % iid, json={'refunded': True})
            self.assertEqual(r.status_code, 401, '%s 不该能退货' % label)

    def test_purchase_can_refund_and_undo(self):
        iid = self._item(paid=100)
        r = self.purchase.post('/api/items/%d/refund' % iid, json={'refunded': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['refunded'], 1)
        self.assertEqual(r.json()['refund_amount'], 100, '默认按实付全退')

        r = self.purchase.post('/api/items/%d/refund' % iid, json={'refunded': False})
        self.assertEqual(r.json()['refunded'], 0)

    def test_refund_amount_can_be_given(self):
        iid = self._item(paid=100)
        r = self.purchase.post('/api/items/%d/refund' % iid,
                               json={'refunded': True, 'refund_amount': 80})
        self.assertEqual(r.json()['refund_amount'], 80)

    def test_refunding_a_missing_item_is_404(self):
        r = self.purchase.post('/api/items/99999/refund', json={'refunded': True})
        self.assertEqual(r.status_code, 404)

    def test_the_round_list_carries_the_flag(self):
        iid = self._item()
        self.purchase.post('/api/items/%d/refund' % iid, json={'refunded': True})
        got = self.purchase.get('/api/rounds/%d/items' % self.rid).json()
        self.assertEqual([i['refunded'] for i in got if i['id'] == iid], [1])


class TestRefundFrontendWiring(unittest.TestCase):
    """界面接线。**确认框必须提到发票会被删** —— 那是不可逆的。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_has_the_toggle(self):
        self.assertIn('async function toggleRefund(it)', self.js)
        self.assertIn("'/refund'", self.js)

    def test_button_says_refund_or_undo(self):
        self.assertIn("it.refunded ? '取消退货' : '退货'", self.js)

    def test_only_purchase_gets_the_button(self):
        self.assertIn('退货是采购人员的事', self.js)
        self.assertIn('if (IS_PURCHASE) {', self.js)

    def test_confirm_warns_about_deleting_the_invoice(self):
        seg = self.js.split('async function toggleRefund(it)')[1].split('async function')[0]
        self.assertIn('发票会被删掉', seg)
        self.assertIn('confirm(', seg)

    def test_it_says_the_record_is_kept(self):
        seg = self.js.split('async function toggleRefund(it)')[1].split('async function')[0]
        self.assertIn('记录会留着', seg)

    def test_the_row_is_marked(self):
        self.assertIn('refundtag', self.js)
        self.assertIn('.refundtag', self.css)
        self.assertIn('tr.refunded', self.css)


if __name__ == '__main__':
    unittest.main(verbosity=2)
