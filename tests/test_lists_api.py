# -*- coding: utf-8 -*-
"""P2 状态流转与清单 API（含发票上传/查看/删除）。"""
import io
import unittest

from fastapi.testclient import TestClient

from app.core import repo
from app.server.api import app
from tests.support import TempDataTest, purchase_client

PDF = b'%PDF-1.4 fake invoice'
ITEM = {
    'name': '充电机', 'spec': '3.3kwh', 'qty': 1, 'unit': '个', 'price': 6200,
    'freight': 0, 'purpose': '高压电池充电', 'applicant': '张三',
    'phone': '13800000000', 'url': '浩泰科技', 'group_name': '电气组', 'paid': 6200,
}


class ApiCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)
        self.rid = self.client.post('/api/rounds', json={'name': '八月采购'}).json()['id']
        self.mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']

    def add(self, **over):
        body = dict(ITEM)
        body.update(over)
        body['member_id'] = self.mid
        return self.client.post('/api/rounds/%d/items' % self.rid, json=body).json()['item']

    def upload(self, iid, name='invoice.pdf', data=PDF):
        return self.client.post('/api/items/%d/invoices' % iid,
                                files={'file': (name, io.BytesIO(data), 'application/pdf')})


class TestMeta(ApiCase):

    def test_meta_exposes_enums(self):
        m = self.client.get('/api/meta').json()
        self.assertEqual(m['order_statuses'], ['待下单', '已下单', '已发货', '已到货'])
        self.assertEqual(m['invoice_statuses'], ['未要', '已要', '已收到'])
        self.assertEqual(m['group'], '电气组')
        self.assertEqual(m['threshold'], 1000.0)


class TestItemStatus(ApiCase):

    def test_defaults(self):
        it = self.add()
        self.assertEqual(it['order_status'], '待下单')
        self.assertEqual(it['invoice_status'], '未要')

    def test_order_status_syncs_arrived_column(self):
        iid = self.add()['id']
        r = self.client.post('/api/items/%d/status' % iid, json={'order_status': '已到货'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['order_status'], '已到货')
        self.assertEqual(r.json()['arrived'], '是', '到货要同步到模板的「是否到货」列')

        r = self.client.post('/api/items/%d/status' % iid, json={'order_status': '已下单'})
        self.assertEqual(r.json()['arrived'], '', '退回未到货要把该列清掉')

    def test_invoice_status_syncs_invoice_column(self):
        iid = self.add()['id']
        r = self.client.post('/api/items/%d/status' % iid, json={'invoice_status': '已收到'})
        self.assertEqual(r.json()['invoice'], '有', '收到发票要同步到模板的「发票」列')
        r = self.client.post('/api/items/%d/status' % iid, json={'invoice_status': '已要'})
        self.assertEqual(r.json()['invoice'], '')

    def test_partial_update_keeps_the_other_field(self):
        iid = self.add()['id']
        self.client.post('/api/items/%d/status' % iid, json={'order_status': '已发货'})
        it = self.client.post('/api/items/%d/status' % iid, json={'invoice_status': '已要'}).json()
        self.assertEqual(it['order_status'], '已发货')
        self.assertEqual(it['invoice_status'], '已要')

    def test_unknown_status_rejected(self):
        iid = self.add()['id']
        self.assertEqual(self.client.post('/api/items/%d/status' % iid,
                                          json={'order_status': '已飞走'}).status_code, 400)
        self.assertEqual(self.client.post('/api/items/%d/status' % iid,
                                          json={'invoice_status': '梦里'}).status_code, 400)

    def test_editing_an_item_does_not_wipe_the_status_columns(self):
        """「改」一次条目不能把发票/是否到货抹掉 —— 表单里根本没有这两个字段。"""
        iid = self.add(name='PCB物料', paid=157.92)['id']
        self.client.post('/api/items/%d/status' % iid,
                         json={'order_status': '已到货', 'invoice_status': '已收到'})

        form = {'member_id': self.mid, 'name': 'PCB物料', 'spec': 'tsal', 'qty': 1,
                'unit': '套', 'price': 158, 'freight': 0, 'purpose': 'tsal板',
                'applicant': '张三', 'phone': '13800000000', 'url': '浩泰科技',
                'group_name': '电气组', 'note': '', 'paid': 157.92}
        r = self.client.put('/api/items/%d' % iid, json=form)
        self.assertEqual(r.status_code, 200, r.text)
        it = r.json()['item']
        self.assertEqual(it['order_status'], '已到货')
        self.assertEqual(it['invoice_status'], '已收到')
        self.assertEqual(it['arrived'], '是', '「是否到货」被编辑操作抹掉了')
        self.assertEqual(it['invoice'], '有', '「发票」被编辑操作抹掉了')

    def test_partial_edit_only_touches_what_was_sent(self):
        iid = self.add(name='PCB物料', paid=157.92)['id']
        repo.update_item(iid, {'has_contract': '有', 'note': '张勇已付款'})
        self.assertEqual(self.client.put('/api/items/%d' % iid,
                                         json={'qty': 3}).status_code, 200)
        it = repo.get_item(iid)
        self.assertEqual(it['has_contract'], '有')
        self.assertEqual(it['note'], '张勇已付款')
        self.assertEqual(it['name'], 'PCB物料')

    def test_empty_edit_is_rejected(self):
        iid = self.add()['id']
        self.assertEqual(self.client.put('/api/items/%d' % iid, json={}).status_code, 400)

    def test_missing_item_is_404(self):
        self.assertEqual(self.client.post('/api/items/999/status',
                                          json={'order_status': '已下单'}).status_code, 404)

    def test_status_change_allowed_after_locking(self):
        """锁定只是不让组员改条目内容；下单与收发票都发生在锁定之后。"""
        iid = self.add()['id']
        self.client.post('/api/rounds/%d/status' % self.rid, json={'status': 'locked'})
        r = self.client.post('/api/items/%d/status' % iid, json={'order_status': '已下单'})
        self.assertEqual(r.status_code, 200, '锁定后仍应能改下单状态')
        self.assertEqual(r.json()['order_status'], '已下单')
        self.assertEqual(self.upload(iid).status_code, 200, '锁定后仍应能传发票')


class TestPendingLists(ApiCase):

    def test_pending_order_shrinks_as_things_get_ordered(self):
        a = self.add(name='充电机')['id']
        b = self.add(name='绝缘手套', url='速迫')['id']
        self.assertEqual([i['id'] for i in
                          self.client.get('/api/rounds/%d/pending-order' % self.rid).json()], [a, b])

        self.client.post('/api/items/%d/status' % a, json={'order_status': '已下单'})
        self.assertEqual([i['id'] for i in
                          self.client.get('/api/rounds/%d/pending-order' % self.rid).json()], [b])

        self.client.post('/api/items/%d/status' % b, json={'order_status': '已到货'})
        self.assertEqual(self.client.get('/api/rounds/%d/pending-order' % self.rid).json(), [])

    def test_pending_invoice_is_the_missing_receipt_list(self):
        a = self.add(name='充电机')['id']
        b = self.add(name='绝缘手套', url='速迫')['id']
        self.assertEqual(len(self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json()), 2)

        self.client.post('/api/items/%d/status' % a, json={'invoice_status': '已要'})
        self.assertEqual(len(self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json()), 2,
                         '「已要」还没拿到，仍算缺')

        self.client.post('/api/items/%d/status' % a, json={'invoice_status': '已收到'})
        got = self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json()
        self.assertEqual([i['id'] for i in got], [b])

    def test_lists_are_per_round(self):
        other = self.client.post('/api/rounds', json={'name': '九月采购'}).json()['id']
        self.add()
        self.assertEqual(len(self.client.get('/api/rounds/%d/pending-order' % self.rid).json()), 1)
        self.assertEqual(self.client.get('/api/rounds/%d/pending-order' % other).json(), [])


class TestInvoiceUpload(ApiCase):

    def test_upload_renames_and_marks_received(self):
        it = self.add(name='PCB物料', paid=157.92)
        r = self.upload(it['id'])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['state'], 'new')
        self.assertEqual(body['invoice']['stored_name'], 'DQ-PCB物料-157.92.pdf')
        self.assertEqual(body['invoice']['rel_path'], 'invoices/八月采购/DQ-PCB物料-157.92.pdf')
        self.assertEqual(body['item']['invoice_status'], '已收到')
        self.assertEqual(body['item']['invoice'], '有')

    def test_duplicate_upload_is_reported_not_duplicated(self):
        it = self.add(name='PCB物料', paid=157.92)
        first = self.upload(it['id']).json()['invoice']
        r = self.upload(it['id'])
        self.assertEqual(r.json()['state'], 'duplicate')
        self.assertEqual(r.json()['invoice']['id'], first['id'], '应复用已登记的那条记录')

        got = self.client.get('/api/rounds/%d/invoices' % self.rid).json()
        self.assertEqual(len(got), 1, '重复上传不该多建记录')
        self.assertEqual(got[0]['stored_name'], 'DQ-PCB物料-157.92.pdf')

    def test_bad_extension_rejected(self):
        iid = self.add()['id']
        r = self.client.post('/api/items/%d/invoices' % iid,
                             files={'file': ('virus.exe', io.BytesIO(b'MZ'), 'application/octet-stream')})
        self.assertEqual(r.status_code, 400)
        self.assertIn('不支持', r.json()['detail'])

    def test_empty_file_rejected(self):
        iid = self.add()['id']
        r = self.client.post('/api/items/%d/invoices' % iid,
                             files={'file': ('a.pdf', io.BytesIO(b''), 'application/pdf')})
        self.assertEqual(r.status_code, 400)

    def test_upload_to_missing_item_is_404(self):
        self.assertEqual(self.upload(999).status_code, 404)

    def test_upload_clears_item_from_pending_invoice(self):
        iid = self.add(name='PCB物料', paid=157.92)['id']
        self.assertEqual(len(self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json()), 1)
        self.upload(iid)
        self.assertEqual(self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json(), [])


class TestInvoiceFileAndDelete(ApiCase):

    def test_preview_is_inline_and_download_is_attachment(self):
        it = self.add(name='PCB物料', paid=157.92)
        inv_id = self.upload(it['id']).json()['invoice']['id']

        inline = self.client.get('/api/invoices/%d/file' % inv_id)
        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline.content, PDF)
        self.assertEqual(inline.headers['content-type'], 'application/pdf')
        self.assertIn('inline', inline.headers['content-disposition'])

        dl = self.client.get('/api/invoices/%d/file?download=1' % inv_id)
        self.assertIn('attachment', dl.headers['content-disposition'])

    def test_chinese_filename_is_encoded_not_dropped(self):
        it = self.add(name='PCB物料', paid=157.92)
        inv_id = self.upload(it['id']).json()['invoice']['id']
        cd = self.client.get('/api/invoices/%d/file?download=1' % inv_id).headers['content-disposition']
        self.assertTrue(any(ch.isdigit() for ch in cd.split('filename')[-1]),
                        '中文文件名应当被编码进 filename*：%r' % cd)

    def test_missing_invoice_is_404(self):
        self.assertEqual(self.client.get('/api/invoices/999/file').status_code, 404)

    def test_delete_removes_record_and_file(self):
        it = self.add(name='PCB物料', paid=157.92)
        up = self.upload(it['id']).json()
        inv_id = up['invoice']['id']
        self.assertEqual(self.client.delete('/api/invoices/%d' % inv_id).status_code, 200)
        self.assertEqual(self.client.get('/api/rounds/%d/invoices' % self.rid).json(), [])
        self.assertEqual(self.client.get('/api/invoices/%d/file' % inv_id).status_code, 404)

    def test_delete_missing_is_404(self):
        self.assertEqual(self.client.delete('/api/invoices/999').status_code, 404)

    def test_deleting_the_last_invoice_puts_the_item_back_on_the_missing_list(self):
        it = self.add(name='PCB物料', paid=157.92)
        inv_id = self.upload(it['id']).json()['invoice']['id']
        self.assertEqual(self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json(), [])

        self.client.delete('/api/invoices/%d' % inv_id)
        got = self.client.get('/api/rounds/%d/pending-invoice' % self.rid).json()
        self.assertEqual([i['id'] for i in got], [it['id']])
        self.assertEqual(got[0]['invoice_status'], '未要')
        self.assertEqual(got[0]['invoice'], '')


class TestRoundDetailPayload(ApiCase):

    def test_detail_carries_invoice_and_pending_info(self):
        it = self.add(name='PCB物料', paid=157.92)
        self.upload(it['id'])
        d = self.client.get('/api/rounds/%d' % self.rid).json()
        self.assertEqual(len(d['invoices']), 1)
        # JSON 对象的键一定是字符串，前端取用时也按 String(id) 取
        self.assertEqual(d['invoice_counts'], {str(it['id']): 1})
        self.assertEqual(d['pending_invoice'], [])
        self.assertEqual(d['pending_order'], [it['id']])
        self.assertEqual(d['split_ids'], {'low': [], 'high': [it['id']]})
        self.assertEqual(d['items'][0]['invoice_status'], '已收到')


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestSwitchingRoundSafely(unittest.TestCase):
    """切轮次失败时**不能**留下「下拉是 A、表格是 B」的状态。

    那时人以为在看新轮次，实际改的是上一轮的行 —— 这是真会改错数据的一类错。
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.js = (Path(__file__).resolve().parent.parent / 'app/web/app.js').read_text(
            encoding='utf-8')

    def test_a_failed_switch_puts_the_dropdown_back(self):
        seg = self.js.split('async function loadRound(')[1].split('\n}')[0]
        self.assertIn('try {', seg)
        self.assertIn('catch (e)', seg)
        self.assertIn("sel.value = state.round ? String(state.round.id) : ''", seg)

    def test_it_tells_the_user_instead_of_failing_silently(self):
        seg = self.js.split('async function loadRound(')[1].split('\n}')[0]
        self.assertIn('切轮次失败', seg)

    def test_a_stale_response_is_still_discarded(self):
        """慢请求后到不能覆盖新轮次的数据。"""
        seg = self.js.split('async function loadRound(')[1].split('\n}')[0]
        self.assertIn('if (seq !== _roundSeq) return;', seg)
