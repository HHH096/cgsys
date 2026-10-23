# -*- coding: utf-8 -*-
"""外部上传合并：`POST /api/merge/custom.pdf`。

给谁用：商家把票开错了、退回去重开，回来的是几张新的 PDF / 照片，
还没进任何一轮的账，但人当场就要合起来打印。

所以这个端点的三条硬规矩：
* **不入库** —— 合并完系统里的发票表、条目金额都不许变（返工票不留痕）
* **背面靠自动匹配** —— 金额配得上的那条采购，信息印上去；配不上留空手写
* **图片照样收** —— jpg/png/bmp/tif 先转 PDF 再合，跟系统内那套一致
"""
import io
import unittest

import fitz

from app import config
from app.core import repo
from app.server.api import app
from tests.support import TempDataTest, purchase_client


def money_pdf(amount):
    """造一张**读得出金额**的 PDF。

    只用 `¥` + 两位小数（`extract_amounts` 的兜底分支），
    不写「价税合计」那种中文表头 —— 免得测试卡在字体上。
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((12, 40), 'Total ¥%.2f' % amount, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def png_bytes(w=300, h=200):
    """造一张真 PNG（图片票，转 PDF 后没有文字层）。"""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(200)
    return pix.tobytes('png')


class CustomMergeCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)
        self.rid = self.client.post('/api/rounds', json={'name': '十月采购'}).json()['id']
        self.mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']

    def add(self, name, paid=100.0, total=None, rid=None):
        body = {'member_id': self.mid, 'name': name, 'unit': '个', 'qty': 1,
                'purpose': '箱检', 'price': paid, 'applicant': '张三',
                'group_name': '电气组', 'paid': paid}
        if total is not None:
            body['total'] = total
        r = self.client.post('/api/rounds/%d/items' % (rid or self.rid), json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['item']

    def merge(self, blobs, **form):
        files = [('files', (n, io.BytesIO(d), 'application/octet-stream'))
                 for n, d in blobs]
        return self.client.post('/api/merge/custom.pdf', files=files, data=form)

    def open_pdf(self, resp):
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers['content-type'], 'application/pdf')
        self.assertEqual(resp.content[:4], b'%PDF')
        return fitz.open(stream=resp.content, filetype='pdf')


class TestGuards(CustomMergeCase):

    def test_anonymous_is_401(self):
        from fastapi.testclient import TestClient
        anon = TestClient(app)
        with anon:
            r = anon.post('/api/merge/custom.pdf',
                          files={'files': ('a.pdf', io.BytesIO(money_pdf(1)), 'application/pdf')})
        self.assertEqual(r.status_code, 401)

    def test_unsupported_type_is_400(self):
        r = self.merge([('a.txt', b'hello')])
        self.assertEqual(r.status_code, 400)
        self.assertIn('不支持的文件类型', r.json()['detail'])

    def test_broken_image_is_400(self):
        r = self.merge([('a.png', b'not really a png')])
        self.assertEqual(r.status_code, 400)
        self.assertIn('a.png', r.json()['detail'])

    def test_empty_upload_is_400(self):
        r = self.merge([('a.pdf', b'')])
        self.assertEqual(r.status_code, 400)

    def test_oversized_file_is_400(self):
        from unittest import mock
        big = b'x' * 1024
        with mock.patch('app.server.api.MAX_UPLOAD_BYTES', 512):
            r = self.merge([('big.pdf', big)])
        self.assertEqual(r.status_code, 400)
        self.assertIn('文件太大', r.json()['detail'])


class TestMergedOutput(CustomMergeCase):

    def test_two_pdfs_merge_with_back(self):
        doc = self.open_pdf(self.merge(
            [('a.pdf', money_pdf(10)), ('b.pdf', money_pdf(20))],
            per_page='2', back='1', frame='1'))
        # 2 张票、每页 2 张 → 1 组 → 正面 + 背面 = 2 页
        self.assertEqual(doc.page_count, 2)

    def test_without_back_only_front(self):
        doc = self.open_pdf(self.merge(
            [('a.pdf', money_pdf(10)), ('b.pdf', money_pdf(20))],
            per_page='2', back='0'))
        self.assertEqual(doc.page_count, 1)

    def test_image_is_converted_and_merged(self):
        doc = self.open_pdf(self.merge([('照片.png', png_bytes())], back='0'))
        self.assertGreaterEqual(doc.page_count, 1)

    def test_image_and_pdf_mix(self):
        doc = self.open_pdf(self.merge(
            [('a.pdf', money_pdf(10)), ('b.png', png_bytes())],
            per_page='2', back='0'))
        self.assertEqual(doc.page_count, 1)


class TestBackMatching(CustomMergeCase):

    def test_back_matches_item_by_amount(self):
        self.add('充电机', paid=22.0)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                       per_page='1', back='1', round_id=str(self.rid)))
        text = doc[1].get_text()
        self.assertIn('充电机', text, '背面应印上金额匹配到的那条采购')
        self.assertIn('电气组', text)

    def test_unmatched_upload_still_merges_with_blank_back(self):
        """配不上就留空 —— 不能因为匹配不上就出不了 PDF。"""
        self.add('充电机', paid=999.0)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                       per_page='1', back='1', round_id=str(self.rid)))
        self.assertEqual(doc.page_count, 2)
        self.assertNotIn('充电机', doc[1].get_text())

    def test_without_round_id_still_merges(self):
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))], back='1'))
        self.assertEqual(doc.page_count, 2)


class TestMultiRoundMatching(CustomMergeCase):
    """背面可以**去好几轮里配** —— 返工那批票常常是上个月采购的。

    只勾一轮时不许跨轮误配；勾两轮时两轮的条目都能配上；
    一轮不勾就谁都不配（留空），但不影响出 PDF。
    """

    def other_round(self, name):
        return self.client.post('/api/rounds', json={'name': name}).json()['id']

    def test_matches_from_the_second_selected_round(self):
        r2 = self.other_round('九月采购')
        self.add('九月那一台', paid=33.0, rid=r2)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(33.0))],
                                       per_page='1', back='1',
                                       round_ids='%d,%d' % (self.rid, r2)))
        self.assertIn('九月那一台', doc[1].get_text())

    def test_other_round_not_matched_when_not_selected(self):
        r2 = self.other_round('九月采购')
        self.add('九月那一台', paid=33.0, rid=r2)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(33.0))],
                                       per_page='1', back='1',
                                       round_ids=str(self.rid)))
        self.assertNotIn('九月那一台', doc[1].get_text())

    def test_no_round_selected_leaves_back_blank(self):
        self.add('充电机', paid=22.0)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                       per_page='1', back='1', round_ids=''))
        self.assertEqual(doc.page_count, 2)
        self.assertNotIn('充电机', doc[1].get_text())

    def test_single_round_id_still_works(self):
        """老写法（只传 round_id）不能因为这次改动就失效。"""
        self.add('充电机', paid=22.0)
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                       per_page='1', back='1', round_id=str(self.rid)))
        self.assertIn('充电机', doc[1].get_text())

    def test_junk_round_ids_is_ignored(self):
        doc = self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                       per_page='1', back='1', round_ids='abc,,'))
        self.assertEqual(doc.page_count, 2)


class TestNothingPersisted(CustomMergeCase):

    def test_merge_does_not_touch_the_invoice_table(self):
        """核心承诺：返工票用完即弃，合并完账上一条都不能多。"""
        self.add('充电机', paid=22.0)
        before = len(repo.list_invoices())
        self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                 back='1', round_id=str(self.rid)))
        self.assertEqual(len(repo.list_invoices()), before)

    def test_merge_does_not_change_item_amounts(self):
        it = self.add('充电机', paid=22.0)
        before = repo.get_item(it['id'])
        self.open_pdf(self.merge([('a.pdf', money_pdf(22.0))],
                                 back='1', round_id=str(self.rid)))
        after = repo.get_item(it['id'])
        self.assertEqual(before['paid'], after['paid'])
        self.assertEqual(before['rev'], after['rev'], '匹配是只读的，不该推版本号')


if __name__ == '__main__':
    unittest.main()
