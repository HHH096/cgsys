# -*- coding: utf-8 -*-
"""照片 / 扫描件的票面金额：用 OCR 认一遍（认不出就让人手填）。

图片发票没有文字层，`read_amount()` 什么也读不到。OCR 是唯一的兜底，
但它**会认错字** —— 实测「便携式电脑桌」那张票，OCR 把「¥174.00」认成
`4174. 00`（¥ 认成 4 粘上去了）、把「合计」认成「吐 计」、把小数点的位置读出个空格。

所以这里的规矩是：**宁可认不出，不可认错** ——
认出来也只当建议值（界面上要人核对），对不上就直接返回 None。
本文件里的 `DESKTOP_OCR` 就是那张票的真实 OCR 结果，原样留着重现问题。
"""

import io
import unittest
from pathlib import Path
from unittest import mock

from app.core import invoicepdf, repo
from tests.support import EntryCase, TempDataTest

# 「便携式电脑桌」那张票的真实 OCR 输出（错字、空格都照抄，别"修好看"）
DESKTOP_OCR = """固英闭固
发
票号码:26352000002311081891
开票日期:2026
年10 月08 日
贤|名称: 欧科智能技术(广州)有限公司
| 名称: 丰泽区拉闸百货商行
|统一社会信用代码/纳税人识别号;9144011 1MAEQCLBO9H
顽目名称
规格型号
单
位
数
量
单
价
金
额
“税率/征收率
税颜
k 家具#折叠桌
71450
张
1
172. 277227722772
172. 28
1%
1:72
吐
计
¥172. 28
¥1. 72
价赊合计
( 大写〉
CQ椿佰染拾肆圆整
(小写)4174. 00
开票
人:黄飞飞
"""


class TestPickFromOcrText(unittest.TestCase):

    def pick(self, text, expect=None):
        return invoicepdf.amount_from_ocr_text(text, expect)

    # ---------------------------------------------------------- 真实那张票

    def test_the_desktop_ticket_reads_174(self):
        """票面是 ¥174.00，OCR 认成 4174.00 —— 拿条目合计一对照就能挑对。"""
        self.assertEqual(self.pick(DESKTOP_OCR, expect=179), 174.0)

    def test_it_keeps_4174_when_that_is_what_was_meant(self):
        """真要是四千多的采购，就不该被"修"成 174。"""
        self.assertEqual(self.pick(DESKTOP_OCR, expect=4200), 4174.0)

    def test_it_refuses_when_nothing_matches(self):
        """两个候选都跟这条采购对不上 → 不填，让人自己看票。"""
        self.assertIsNone(self.pick(DESKTOP_OCR, expect=999))

    def test_without_something_to_compare_it_will_not_guess(self):
        """没有条目金额可对照、候选又不止一个 → 拿不准就别填。"""
        self.assertIsNone(self.pick(DESKTOP_OCR))

    # ---------------------------------------------------------- 常见写法

    def test_the_standard_e_invoice_wording(self):
        text = '价税合计（大写）叁拾叁圆整\n（小写）¥33.00\n'
        self.assertEqual(self.pick(text, expect=22), 33.0)
        self.assertEqual(self.pick(text), 33.0)

    def test_the_yuan_sign_read_as_the_letter_y(self):
        self.assertEqual(self.pick('价税合计（小写）Y16.00', expect=16), 16.0)

    def test_amount_and_tax_add_up_to_the_total(self):
        """「合计」那行有两个数（金额 + 税额）—— 加起来就是价税合计。"""
        text = '合计 ¥172. 28 ¥1. 72\n'
        self.assertEqual(self.pick(text, expect=179), 174.0)
        # 没有「小写」行也照样能算出来
        self.assertEqual(self.pick(text), 174.0)

    def test_the_total_wins_when_it_matches_the_sum(self):
        text = '合计 ¥172.28 ¥1.72\n价税合计（小写）¥174.00\n'
        self.assertEqual(self.pick(text, expect=179), 174.0)

    def test_spaces_between_chinese_characters(self):
        """OCR 爱在中文字之间插空格（`价 税 合 计`、`小 写`）——
        关键词得先压掉空白再比，否则整张票都解析不出（线上真踩过，
        命令行 tesseract 那次输出就把这张票解析成了 None）。"""
        text = ('『 家 具 # 折 蔡 桌 71450 张 1 172. 277227722772 172. 28 1% 1. 72\n'
                '员 计 ¥172. 28 ¥1.72\n'
                '价 税 合 计 ( 大 写 ) ( 之 不 何 弦 拾 肆 圆 整 ( 小 写 ) 174. 00\n')
        self.assertEqual(self.pick(text, expect=179), 174.0)

    def test_a_lone_candidate_is_used(self):
        self.assertEqual(self.pick('价税合计（小写）¥28.74', ), 28.74)

    def test_a_long_digit_string_is_not_money(self):
        """票号、日期不能被当成金额。"""
        text = '发票号码:26352000002311081891\n开票日期:2026年10月08日\n'
        self.assertIsNone(self.pick(text, expect=179))

    def test_unit_price_typo_is_not_taken_as_the_total(self):
        """单价那一长串 `172. 277227722772` 不是金额。"""
        line = '家具#折叠桌 1 172. 277227722772'
        self.assertEqual(invoicepdf._moneys(line), [])

    def test_empty_text(self):
        self.assertIsNone(self.pick(''))
        self.assertIsNone(self.pick(None))


class TestTrimLeading(unittest.TestCase):
    """OCR 把 ¥ 认成数字粘上去的修正。"""

    def test_it_drops_a_four_digit_first_char(self):
        self.assertEqual(invoicepdf._trim_leading(4174.0), 174.0)
        self.assertEqual(invoicepdf._trim_leading(1145.5), 145.5)

    def test_it_leaves_three_digit_numbers_alone(self):
        """174 去掉首位成了 74 —— 那是瞎猜，不是修正。"""
        self.assertIsNone(invoicepdf._trim_leading(174.0))
        self.assertIsNone(invoicepdf._trim_leading(99.5))


class TestOcrAvailability(TempDataTest):
    """本机有没有 OCR 引擎，以及关键时刻会不会安静地放弃。"""

    def test_it_looks_for_the_tesseract_binary(self):
        with mock.patch('shutil.which', return_value='/usr/bin/tesseract'):
            self.assertTrue(invoicepdf.ocr_available())
        with mock.patch('shutil.which', return_value=None):
            self.assertFalse(invoicepdf.ocr_available())

    def test_without_tesseract_it_gives_up_quietly(self):
        """本地开发机一般没装 —— 那就直接让人手填，别报错。"""
        with mock.patch('shutil.which', return_value=None):
            self.assertIsNone(invoicepdf.read_amount_ocr('/anything.pdf'))

    def test_a_missing_file_is_just_none(self):
        with mock.patch('shutil.which', return_value='/usr/bin/tesseract'):
            self.assertIsNone(invoicepdf.read_amount_ocr('/definitely/not/here.pdf'))

    def test_a_crash_or_timeout_is_just_none(self):
        """tesseract 挂了 / 超时，也不能把整个上传搞失败。"""
        path = Path(self._tmp.name) / 'x.pdf'
        path.write_bytes(make_pdf_bytes())
        with mock.patch('shutil.which', return_value='/usr/bin/tesseract'), \
                mock.patch('subprocess.run', side_effect=RuntimeError('boom')):
            self.assertIsNone(invoicepdf.read_amount_ocr(str(path)))

    def test_it_feeds_a_rendered_page_to_the_tesseract_command(self):
        """走的是「渲成图 + tesseract 命令行」。

        不用 PyMuPDF 那套 `get_textpage_ocr()`：它只认**import 那一刻**的
        `TESSDATA_PREFIX`，之后再设也没用（线上真踩过，先在命令行设好就成功、
        代码里设就报 No OCR support）。
        """
        path = Path(self._tmp.name) / 't.pdf'
        path.write_bytes(make_pdf_bytes())
        fake = mock.Mock(stdout='价税合计（小写）¥33.00\n'.encode('utf-8'))
        with mock.patch('shutil.which', return_value='/usr/bin/tesseract'), \
                mock.patch('subprocess.run', return_value=fake) as run:
            self.assertEqual(invoicepdf.read_amount_ocr(str(path), expect=33), 33.0)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], '/usr/bin/tesseract')
        self.assertTrue(cmd[1].endswith('.png'))      # 先把那一页渲成图
        self.assertIn('stdout', cmd)                  # 让 tesseract 打到标准输出
        self.assertEqual(cmd[-1], 'chi_sim+eng')
        # 那套 TESSDATA_PREFIX 的绕法应该已经不存在了
        self.assertFalse(hasattr(invoicepdf, '_ensure_tessdata'))


def make_pdf_bytes():
    """一张真 PDF（里面塞张图）—— 测「渲成图 → 交给 tesseract」这条链路。"""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=200, height=150)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 60))
    pix.clear_with(210)
    page.insert_image(fitz.Rect(20, 20, 100, 80), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def make_png(w=200, h=150):
    """随便一张真 PNG（OCR 那条路只关心"是不是图片"）。"""
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(240)
    return pix.tobytes('png')


class TestOcrOnUpload(EntryCase):
    """接口层：照片走 OCR；电子发票不浪费这个时间。"""

    def setUp(self):
        super().setUp()
        self.iid = repo.add_item(self.rid, self.alice, {
            'name': '电脑桌', 'qty': 1, 'unit': '张', 'price': 179,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(self.iid, order_status='已到货')

    def upload(self, data=None, name='票.png'):
        data = make_png() if data is None else data
        return self.alice_c.post('/api/items/%d/invoices' % self.iid,
                                 files={'file': (name, io.BytesIO(data), 'image/png')})

    def test_a_photo_gets_the_amount_from_ocr(self):
        with mock.patch('app.server.api.invoicepdf.read_amount_ocr',
                        return_value=174.0) as ocr:
            j = self.upload().json()
        self.assertEqual(j['paid_filled'], 174.0)
        # 前端要照 paid_source 说一句「请核对票面」——OCR 认的可能错
        self.assertEqual(j['paid_source'], 'ocr')
        self.assertFalse(j['need_paid'])
        self.assertEqual(repo.get_item(self.iid)['paid'], 174.0)
        # 消歧要用到这条采购自己算出来的合计
        self.assertEqual(ocr.call_args[1].get('expect'), 179.0)

    def test_when_ocr_gives_up_it_still_asks(self):
        with mock.patch('app.server.api.invoicepdf.read_amount_ocr', return_value=None):
            j = self.upload().json()
        self.assertIsNone(j['paid_filled'])
        self.assertTrue(j['need_paid'])              # 回到「让人手填」
        self.assertIsNone(repo.get_item(self.iid)['paid'])

    def test_an_e_invoice_never_goes_through_ocr(self):
        """有文字层的票读得又准又快，别去跑那慢吞吞的 OCR。"""
        with mock.patch('app.server.api.invoicepdf.read_lines',
                        return_value=[{'name': '家具*折叠桌', 'amount': 172.28,
                                       'tax': 1.72, 'total': 174.0}]), \
                mock.patch('app.server.api.invoicepdf.read_amount_ocr') as ocr:
            j = self.upload().json()
        ocr.assert_not_called()
        self.assertEqual(j['paid_source'], 'pdf')
        self.assertEqual(j['paid_filled'], 174.0)

    def test_re_uploading_an_old_photo_fills_the_amount_at_last(self):
        """老票重传一遍：正好把当初读不出来的金额补上。"""
        with mock.patch('app.server.api.invoicepdf.read_amount_ocr', return_value=None):
            first = self.upload().json()
        self.assertTrue(first['need_paid'])
        with mock.patch('app.server.api.invoicepdf.read_amount_ocr', return_value=174.0):
            again = self.upload().json()
        self.assertEqual(again['state'], 'duplicate')
        self.assertEqual(again['paid_source'], 'ocr')
        self.assertEqual(repo.get_item(self.iid)['paid'], 174.0)


class TestFrontendOcrWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')

    def test_the_toast_says_the_amount_needs_checking(self):
        seg = self.js.split('async function uploadInvoiceFile(')[1].split('\nfunction ')[0]
        self.assertIn("j.paid_source === 'ocr'", seg)
        self.assertIn('请对一下票面', seg)

    def test_the_page_explains_the_ocr_guess(self):
        self.assertIn('OCR', self.html)
        self.assertIn('建议值', self.html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
