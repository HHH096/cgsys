# -*- coding: utf-8 -*-
"""图片发票（jpg/png/bmp/tif）上传后**自动转成 PDF** 再归档。

商家发的截图、手机拍的照片本身都不是 PDF。以前会原样存成 `.jpg`：
命名、预览、合并打印各走一条分支，界面上也认不出是什么。
现在归档时统一转成 PDF，落盘一律 `DQ-物品名-金额.pdf`。

注意（别当成 bug）：**图片没有文字层，转出来的 PDF 一样读不出金额** ——
所以上传完仍会让人补实付金额，这是设计好的提示，不是转换失败。
"""
import io
import struct
import unittest
from pathlib import Path
from unittest import mock

import fitz

from app import config
from app.core import invoicing, repo
from tests.support import EntryCase, TempDataTest, TEST_PDF as PDF


def make_png(w=400, h=300, colour=230):
    """造一张真 PNG（不依赖外部文件，也不用装 Pillow）。"""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(colour)
    return pix.tobytes('png')


def make_jpg(w=400, h=300, colour=150):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(colour)
    return pix.tobytes('jpg', jpg_quality=85)


def make_bmp(w=8, h=6, colour=0x80):
    """手写一张 24 位 BMP —— PyMuPDF 不能生成 bmp，只能自己拼。"""
    row = bytes((colour, colour, colour)) * w
    pad = b'\x00' * ((4 - (w * 3) % 4) % 4)
    px = (row + pad) * h
    header = struct.pack('<2sIHHI', b'BM', 54 + len(px), 0, 0, 54)
    info = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, len(px), 2835, 2835, 0, 0)
    return header + info + px


class TestImageToPdf(TempDataTest):

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月采购')
        self.mid = repo.add_member('李四', '')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': '电容', 'qty': 1, 'unit': '个', 'price': 22,
            'purpose': 'x', 'applicant': '李四', 'paid': 22})

    def store(self, data, name='发票.png'):
        return invoicing.store_invoice(repo.get_item(self.iid), '九月采购', name, data)

    def test_a_png_lands_as_pdf(self):
        rec = self.store(make_png())
        self.assertTrue(rec['converted'], rec)
        self.assertTrue(rec['stored_name'].endswith('.pdf'), rec['stored_name'])
        self.assertTrue(rec['rel_path'].endswith('.pdf'), rec['rel_path'])
        doc = fitz.open(str(rec['abs_path']))
        try:
            self.assertEqual(doc.page_count, 1)
        finally:
            doc.close()

    def test_a_jpg_lands_as_pdf_too(self):
        rec = self.store(make_jpg(), name='微信图片_2026.jpg')
        self.assertTrue(rec['converted'])
        self.assertTrue(rec['stored_name'].endswith('.pdf'), rec['stored_name'])

    def test_a_bmp_is_recognised_by_content(self):
        """BMP 没有内容嗅探的话会被当成"不是图片"，那就白支持了。"""
        rec = self.store(make_bmp(), name='截图.bmp')
        self.assertTrue(rec['converted'])
        self.assertTrue(rec['stored_name'].endswith('.pdf'))

    def test_the_name_keeps_item_and_amount(self):
        """命名还是老规矩 `DQ-物品名-金额.pdf` —— 换成 .jpg 就对不上了。"""
        rec = self.store(make_png(), name='微信图片_2026.jpg')
        self.assertEqual(rec['stored_name'], 'DQ-电容-22.pdf')
        self.assertEqual(rec['orig_name'], '微信图片_2026.jpg')   # 原名留着备查

    def test_the_page_keeps_the_picture_shape(self):
        rec = self.store(make_png(w=800, h=400))
        doc = fitz.open(str(rec['abs_path']))
        try:
            rect = doc[0].rect
        finally:
            doc.close()
        self.assertAlmostEqual(rect.width / rect.height, 2.0, places=2)

    def test_the_same_picture_twice_is_a_duplicate(self):
        """转换必须是确定性的 —— 不然同一张图每传一次就多存一个文件。"""
        self.assertEqual(self.store(make_png())['state'], 'new')
        self.assertEqual(self.store(make_png())['state'], 'duplicate')

    def test_a_different_picture_is_a_new_file(self):
        self.store(make_png(colour=230))
        second = self.store(make_png(colour=120))
        self.assertEqual(second['state'], 'new')
        self.assertEqual(second['stored_name'], 'DQ-电容-22-2.pdf')

    def test_a_broken_image_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.store('这不是图片，是几行字而已'.encode('utf-8'), name='假的.png')
        self.assertIn('打不开', str(ctx.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ValueError):
            self.store(b'', name='空的.png')

    def test_a_pdf_is_left_alone(self):
        """电子发票本来就是 PDF，别去动它。"""
        png = make_png()
        doc = fitz.open(stream=png, filetype=None)
        pdf = doc.convert_to_pdf()
        doc.close()
        rec = self.store(pdf, name='dzfp_26132000003239568481_发票.pdf')
        self.assertFalse(rec['converted'])
        self.assertEqual(rec['stored_name'], 'DQ-电容-22.pdf')

    def test_only_the_pdf_is_kept(self):
        """不留原图 —— 一份发票存两份迟早会打架（谁才是"原件"）。"""
        rec = self.store(make_png())
        folder = Path(rec['abs_path']).parent
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ['DQ-电容-22.pdf'])

    def test_the_image_formats_are_declared(self):
        for ext in ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'):
            self.assertIn(ext, invoicing.IMAGE_EXTS)


class TestUploadImageThroughTheApi(EntryCase):
    """走真接口：上传图片 → 归档成 PDF → 能看、能合并打印。"""

    def setUp(self):
        super().setUp()
        self.iid = self._item('折叠椅', 28.74)

    def _item(self, name, price):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def upload(self, client, data, name='票.png', mime='image/png'):
        return client.post('/api/items/%d/invoices' % self.iid,
                           files={'file': (name, io.BytesIO(data), mime)})

    def test_it_is_stored_as_a_pdf(self):
        r = self.upload(self.alice_c, make_png())
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j['converted'])
        self.assertTrue(j['invoice']['stored_name'].endswith('.pdf'),
                        j['invoice']['stored_name'])
        self.assertTrue(j['invoice']['rel_path'].endswith('.pdf'))
        # 文件真的落盘了，而且是能打开的 PDF
        p = Path(config.DATA_DIR) / j['invoice']['rel_path']
        self.assertTrue(p.exists(), j['invoice']['rel_path'])
        doc = fitz.open(str(p))
        try:
            self.assertEqual(doc.page_count, 1)
        finally:
            doc.close()

    def test_the_item_is_marked_received(self):
        self.upload(self.alice_c, make_png())
        self.assertEqual(repo.get_item(self.iid)['invoice_status'], '已收到')

    def test_it_still_asks_for_the_amount(self):
        """图片没有文字层 → 读不出金额 → 要问。这是预期，不是失败。"""
        j = self.upload(self.alice_c, make_png()).json()
        self.assertTrue(j['need_paid'], j)
        self.assertIsNone(repo.get_item(self.iid)['paid'])

    def test_uploading_the_same_photo_twice_does_not_pile_up(self):
        self.upload(self.alice_c, make_png())
        j = self.upload(self.alice_c, make_png(), name='同一张.png').json()
        self.assertEqual(j['state'], 'duplicate')
        self.assertTrue(j['converted'])
        self.assertEqual(len(repo.invoices_of_item(self.iid)), 1)

    def test_a_broken_image_is_a_bad_request(self):
        r = self.upload(self.alice_c, '这根本不是图片'.encode('utf-8'),
                        name='假的.jpg', mime='image/jpeg')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(repo.invoices_of_item(self.iid), [])

    def test_it_can_be_merged_and_printed(self):
        """合并打印走 PDF 那条分支，图片转出来的票要能正常嵌进去。"""
        self.upload(self.alice_c, make_png())
        r = self.purchase.get('/api/rounds/%d/merged.pdf' % self.rid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('pdf', r.headers['content-type'])
        doc = fitz.open(stream=r.content, filetype='pdf')
        try:
            self.assertEqual(doc.page_count, 2)          # 1 页正面 + 1 页背面
            text = ''.join(doc[i].get_text() for i in range(doc.page_count))
            # 嵌入失败时排版引擎会画一块红框并写上「失败」字样
            self.assertNotIn('失败', text)
            self.assertNotIn('无法打开', text)
        finally:
            doc.close()

    def test_it_can_be_previewed(self):
        j = self.upload(self.alice_c, make_png()).json()
        r = self.purchase.get('/api/invoices/%d/file' % j['invoice']['id'])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers['content-type'].split(';')[0], 'application/pdf')


class TestFrontendWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')

    def test_images_are_accepted_by_the_picker(self):
        after = self.html.split('id="invoiceInput"')[1]
        accept = after.split('accept="')[1].split('"')[0]
        for ext in ('.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.tif'):
            self.assertIn(ext, accept, accept)

    def test_the_page_says_images_are_converted(self):
        self.assertIn('图片', self.html)
        self.assertIn('自动转成 PDF', self.html)

    def test_a_converted_upload_says_so(self):
        seg = self.js.split('async function uploadInvoiceFile(')[1].split('\nfunction ')[0]
        self.assertIn('j.converted', seg)
        self.assertIn('图片已自动转成 PDF', seg)


class TestAmountPriority(EntryCase):
    """一票一单时**优先认「价税合计（小写）」**，别拿"金额 + 税额"去拼。

    用户报过：那张票的税额列没读出来，¥29.00 被系统认成了 ¥28.71。
    价税合计（小写）是票面自己算好的总额，最可靠；拼两列只是退路。
    """

    def setUp(self):
        super().setUp()
        self.iid = repo.add_item(self.rid, self.alice, {
            'name': '硅胶管', 'qty': 2, 'unit': '根', 'price': 14.355,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(self.iid, order_status='已下单')

    def upload(self):
        return self.alice_c.post('/api/items/%d/invoices' % self.iid,
                                 files={'file': ('p.pdf', io.BytesIO(PDF), 'application/pdf')})

    def test_the_ticket_total_wins_over_amount_plus_tax(self):
        """税额列读丢时（明细只有 28.71），也要认票面的 ¥29.00。"""
        with mock.patch('app.server.api.invoicepdf.read_lines',
                        return_value=[{'name': '橡胶制品*硅胶管', 'amount': 28.71,
                                       'tax': 0.0, 'total': 28.71}]), \
                mock.patch('app.server.api.invoicepdf.read_amount', return_value=29.0):
            j = self.upload().json()
        self.assertEqual(j['paid_filled'], 29.0)
        self.assertEqual(repo.get_item(self.iid)['paid'], 29.0)

    def test_it_falls_back_to_amount_plus_tax(self):
        """票面读不到价税合计时，退回「金额 + 税额」。"""
        with mock.patch('app.server.api.invoicepdf.read_lines',
                        return_value=[{'name': '橡胶制品*硅胶管', 'amount': 28.71,
                                       'tax': 0.29, 'total': 29.0}]), \
                mock.patch('app.server.api.invoicepdf.read_amount', return_value=None), \
                mock.patch('app.server.api.invoicepdf.read_amount_ocr', return_value=None):
            j = self.upload().json()
        self.assertEqual(j['paid_filled'], 29.0)

    def test_a_multi_line_ticket_still_goes_to_review(self):
        """一票多单不受影响：还是弹核对框，不自动定总额。"""
        lines = [{'name': '电容22uF', 'amount': 21.78, 'tax': 0.22, 'total': 22.0},
                 {'name': '电容10uF', 'amount': 10.89, 'tax': 0.11, 'total': 11.0}]
        with mock.patch('app.server.api.invoicepdf.read_lines', return_value=lines), \
                mock.patch('app.server.api.invoicepdf.read_amount', return_value=33.0):
            j = self.upload().json()
        self.assertTrue(j['need_review'])

    def test_the_file_is_renamed_with_the_amount(self):
        """归档文件也按金额命名：`DQ-硅胶管-29.pdf`，不是没金额段的 `DQ-硅胶管.pdf`。

        归档发生在识别之前，那会儿条目上还没金额，所以识别完必须改一次名。
        """
        with mock.patch('app.server.api.invoicepdf.read_lines', return_value=[]), \
                mock.patch('app.server.api.invoicepdf.read_amount', return_value=29.0):
            j = self.upload().json()
        name = j['invoice']['stored_name']
        self.assertTrue(name.endswith('-29.pdf'), name)
        self.assertTrue(Path(j['invoice']['rel_path']).name.endswith('-29.pdf'))
        # 数据库里记的路径也要跟着变，不然界面上的「看/存」就找不到文件
        p = Path(config.DATA_DIR) / j['invoice']['rel_path']
        self.assertTrue(p.exists(), j['invoice']['rel_path'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
