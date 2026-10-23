# -*- coding: utf-8 -*-
"""P3 合并打印接口：/api/rounds/{id}/merged.pdf。"""
import io
import unittest
from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from app import config
from app.core import repo
from app.server.api import app
from tests.support import TempDataTest, purchase_client

ROOT = Path(__file__).resolve().parent.parent
BACK_LABELS = ['物品名称', '用途', '组别', '付款日期', '归属表格']


def real_pdf_bytes(text='INVOICE'):
    """造一张真正能被打开的 PDF（假字节流会让 fitz 打不开，测不出嵌入）。"""
    doc = fitz.open()
    page = doc.new_page(width=220, height=300)
    page.insert_text((10, 30), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


class MergeCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)
        self.rid = self.client.post('/api/rounds', json={'name': '八月采购'}).json()['id']
        self.mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']

    def add(self, name, **over):
        body = {'member_id': self.mid, 'name': name, 'unit': '个', 'qty': 1,
                'purpose': '箱检', 'price': 100, 'applicant': '张三', 'group_name': '电气组'}
        body.update(over)
        r = self.client.post('/api/rounds/%d/items' % self.rid, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['item']

    def upload(self, iid, name='发票.pdf', text='INVOICE'):
        r = self.client.post('/api/items/%d/invoices' % iid,
                             files={'file': (name, io.BytesIO(real_pdf_bytes(text)), 'application/pdf')})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['invoice']

    def merged(self, **params):
        return self.client.get('/api/rounds/%d/merged.pdf' % self.rid, params=params)

    def open_merged(self, resp):
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers['content-type'], 'application/pdf')
        self.assertEqual(resp.content[:4], b'%PDF')
        return fitz.open(stream=resp.content, filetype='pdf')


class TestGuards(MergeCase):

    def test_no_invoices_is_400_with_a_useful_message(self):
        self.add('充电机')
        r = self.merged()
        self.assertEqual(r.status_code, 400)
        self.assertIn('还没有发票', r.json()['detail'])

    def test_missing_round_is_404(self):
        self.assertEqual(self.client.get('/api/rounds/9999/merged.pdf').status_code, 404)

    def test_missing_archived_file_is_reported(self):
        it = self.add('充电机')
        inv = self.upload(it['id'])
        (config.DATA_DIR / inv['rel_path']).unlink()
        r = self.merged()
        self.assertEqual(r.status_code, 400)
        self.assertIn(inv['stored_name'], r.json()['detail'])


class TestMergedOutput(MergeCase):

    def test_downloads_a_pdf(self):
        it = self.add('充电机')
        self.upload(it['id'])
        resp = self.merged()
        doc = self.open_merged(resp)
        self.assertEqual(doc.page_count, 2, '一张发票 = 正面 + 背面')

    def test_back_carries_the_five_fields(self):
        it = self.add('充电机', purpose='高压电池充电')
        self.upload(it['id'])
        doc = self.open_merged(self.merged())
        text = doc[1].get_text()
        for label in BACK_LABELS:
            self.assertIn(label, text, '背面缺「%s」' % label)
        self.assertIn('充电机', text)
        self.assertIn('高压电池充电', text)
        self.assertIn('电气组', text)
        self.assertIn('月', text)

    def test_front_embeds_the_invoice(self):
        it = self.add('充电机')
        self.upload(it['id'])
        doc = self.open_merged(self.merged())
        self.assertIn('INVOICE', doc[0].get_text())

    def test_per_page_parameter_takes_effect(self):
        for n in ('A', 'B', 'C'):
            self.upload(self.add(n)['id'])
        doc1 = self.open_merged(self.merged(per_page=1))
        self.assertEqual(doc1.page_count, 6)
        doc3 = self.open_merged(self.merged(per_page=3))
        self.assertEqual(doc3.page_count, 2)

    def test_back_can_be_turned_off(self):
        self.upload(self.add('A')['id'])
        doc = self.open_merged(self.merged(back=0))
        self.assertEqual(doc.page_count, 1)

    def test_frame_can_be_turned_off(self):
        self.upload(self.add('A')['id'])
        self.open_merged(self.merged(frame=0))     # 不抛异常即可

    def test_two_invoices_of_one_item_both_included(self):
        it = self.add('PCB物料')
        self.upload(it['id'], name='a.pdf', text='FIRST')
        self.upload(it['id'], name='b.pdf', text='SECOND')
        doc = self.open_merged(self.merged(per_page=2))
        front = doc[0].get_text()
        self.assertIn('FIRST', front)
        self.assertIn('SECOND', front)

    def test_order_follows_item_order_not_upload_order(self):
        first = self.add('第一个')
        second = self.add('第二个')
        # 故意先传第二条第发票
        self.upload(second['id'], text='SECOND')
        self.upload(first['id'], text='FIRST')
        doc = self.open_merged(self.merged(per_page=2))
        front = doc[0].get_text()
        self.assertLess(front.index('FIRST'), front.index('SECOND'),
                        '应按条目顺序排，而不是上传顺序')

    def test_odd_count_last_group_has_a_single_block(self):
        for n in ('A', 'B', 'C'):
            self.upload(self.add(n)['id'])
        doc = self.open_merged(self.merged(per_page=2))
        self.assertEqual(doc.page_count, 4)
        back2 = doc[3].get_text()
        self.assertIn('C', back2)

    def test_output_is_saved_under_export_dir(self):
        self.upload(self.add('A')['id'])
        self.open_merged(self.merged())
        saved = list(config.EXPORT_DIR.glob('发票合并_*.pdf'))
        self.assertEqual(len(saved), 1, '生成的 PDF 应当留在导出目录里')
        self.assertGreater(saved[0].stat().st_size, 0)


class TestFrontendWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')

    def test_controls_exist(self):
        for needle in ('id="mergePdf"', 'id="mergePerPage"', 'id="mergeBack"',
                       'id="mergeFrame"', 'id="mergeHint"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_button_is_bound(self):
        self.assertIn("$('mergePdf').addEventListener('click', mergePdf)", self.js)

    def test_points_at_the_merge_endpoint(self):
        self.assertIn("/merged.pdf?", self.js)

    def test_options_trigger_recount(self):
        self.assertIn("'mergePerPage', 'mergeBack', 'mergeFrame'", self.js)
        self.assertIn('renderMerge', self.js)

    def test_hint_is_updated_when_rendering(self):
        self.assertIn('renderMerge();', self.js)

    def test_the_preview_container_exists(self):
        for needle in ('id="mergePreviewBox"', 'id="mergePreviewFrame"',
                       'id="mergePreviewTitle"', 'id="mergePreviewHint"',
                       'id="mergePreviewDownload"', 'id="mergePreviewPrint"',
                       'id="mergePreviewClose"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_both_entries_feed_the_same_preview(self):
        """系统内合并、本机上传合并 —— 两个入口都要**先看再印**，出口是同一个预览。"""
        for fn in ('async function mergePdf(', 'async function mergeUpload('):
            seg = self.js.split(fn)[1].split('\n}')[0]
            self.assertIn('showMergePreview(', seg, '%s 没走预览' % fn)

    def test_it_no_longer_downloads_or_opens_a_window(self):
        """直接在 fetch 里取 blob 来预览 —— 不能再 window.open 或偷偷存进下载文件夹。"""
        seg = self.js.split('async function mergePdf(')[1].split('\n}')[0]
        self.assertNotIn('window.open(', seg)
        self.assertNotIn('a.download', seg)

    def test_the_upload_entry_no_longer_auto_downloads(self):
        seg = self.js.split('async function mergeUpload(')[1].split('\n}')[0]
        self.assertNotIn('a.click()', seg)
        self.assertNotIn("a.download = '发票合并_外部上传.pdf'", seg)

    def test_the_preview_buttons_are_bound(self):
        for fn in ('closeMergePreview', 'downloadMergePreview', 'printMergePreview'):
            self.assertIn("addEventListener('click', %s)" % fn, self.js)

    def test_the_blob_url_is_released(self):
        """每次预览先放掉上一份、关闭时也放掉，否则每合一次漏几 MB。"""
        self.assertIn('URL.revokeObjectURL(_mergePreviewUrl)', self.js)
        self.assertIn('closeMergePreview();', self.js)

    def test_the_preview_hides_by_default(self):
        """没合过之前不该占着版面。"""
        self.assertIn('id="mergePreviewBox" class="preview-box" hidden', self.html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
