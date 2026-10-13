# -*- coding: utf-8 -*-
"""发票合并打印的排版引擎：页数、A4 尺寸、背面五个字段、奇数张处理。"""
import tempfile
import unittest
from pathlib import Path

import fitz

from app.core import invoicepdf
from app.core.invoicepdf import A4_H, A4_W, MergeOptions

BACK_LABELS = ['物品名称', '用途', '组别', '付款日期', '归属表格']


def make_pdf(path, w=220, h=300, text='INVOICE'):
    """造一张能打开的小 PDF 当发票。"""
    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    page.insert_text((10, 30), text, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


class PdfCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_entry(self, name, filename=None, **item_over):
        path = make_pdf(self.root / (filename or ('%s.pdf' % name)))
        item = {'name': name, 'purpose': '箱检', 'group_name': '电气组',
                'paid': 100.0, 'total': 100.0}
        item.update(item_over)
        invoice = {'invoice_no': '', 'invoice_date': '', 'amount': None, 'seller': ''}
        invoice.update(item_over.pop('_invoice', {}))
        return {'item': item, 'invoice': invoice, 'path': path}

    def build(self, entries, out='out.pdf', **opts):
        out_path = self.root / out
        path, stats = invoicepdf.build_merged_pdf(entries, out_path, MergeOptions(**opts))
        return fitz.open(str(path)), path, stats

    def back_text(self, doc, page_index=1):
        return doc[page_index].get_text()


class TestPageStructure(PdfCase):

    def test_two_invoices_make_two_pages(self):
        entries = [self.make_entry('钮子开关'), self.make_entry('螺丝刀')]
        doc, path, stats = self.build(entries)
        self.assertEqual(doc.page_count, 2, '一组 = 正面 + 背面')
        self.assertEqual(stats['groups'], 1)
        self.assertEqual(stats['invoices'], 2)
        self.assertTrue(path.exists())

    def test_odd_count_still_pairs_up(self):
        """3 张、每页 2 张 → 2 组 → 4 页；最后一张单独占半页。"""
        entries = [self.make_entry('A'), self.make_entry('B'), self.make_entry('C')]
        doc, _, stats = self.build(entries)
        self.assertEqual(stats['groups'], 2)
        self.assertEqual(doc.page_count, 4)

    def test_page_size_is_a4(self):
        doc, _, _ = self.build([self.make_entry('A')])
        for page in doc:
            self.assertAlmostEqual(page.rect.width, A4_W, places=1)
            self.assertAlmostEqual(page.rect.height, A4_H, places=1)

    def test_per_page_changes_group_count(self):
        entries = [self.make_entry(str(i)) for i in range(4)]
        doc, _, stats = self.build(entries, per_page=1)
        self.assertEqual(stats['groups'], 4)
        self.assertEqual(doc.page_count, 8)

        doc3, _, stats3 = self.build(entries, out='o3.pdf', per_page=3)
        self.assertEqual(stats3['groups'], 2)
        self.assertEqual(doc3.page_count, 4)

    def test_without_back_only_front_pages(self):
        entries = [self.make_entry('A'), self.make_entry('B')]
        doc, _, stats = self.build(entries, make_back=False)
        self.assertEqual(doc.page_count, 1)
        self.assertFalse(stats['make_back'])

    def test_footer_is_off_by_default(self):
        doc, _, _ = self.build([self.make_entry('A')])
        self.assertNotIn('第 1 组', doc[0].get_text())

    def test_footer_can_be_turned_on(self):
        doc, _, _ = self.build([self.make_entry('A'), self.make_entry('B')], show_footer=True)
        self.assertIn('正面（发票）', doc[0].get_text())
        self.assertIn('背面（采购信息版面）', doc[1].get_text())


class TestBackLayout(PdfCase):

    def test_back_has_the_five_labels(self):
        doc, _, _ = self.build([self.make_entry('充电机')])
        text = self.back_text(doc)
        for label in BACK_LABELS:
            self.assertIn(label, text, '背面缺少「%s」' % label)

    def test_item_name_is_the_raw_value(self):
        """物品名称只取「物料名称/品牌」原值，不附加规格。"""
        doc, _, _ = self.build([self.make_entry('充电机', spec='3.3kwh')])
        self.assertIn('充电机', self.back_text(doc))

    def test_pay_date_is_left_blank_for_handwriting(self):
        doc, _, _ = self.build([self.make_entry('A')])
        text = self.back_text(doc)
        self.assertIn('付款日期', text)
        # 标签后面不应有内容
        idx = text.index('付款日期')
        tail = text[idx + len('付款日期'):].lstrip('：: \n')
        self.assertTrue(tail.startswith('组别') or tail.startswith('归属表格'),
                        '付款日期栏应当留空，后面接的是下一个标签，实际是 %r' % tail[:20])

    def test_belong_column_prints_the_month_word(self):
        doc, _, _ = self.build([self.make_entry('A')])
        text = self.back_text(doc)
        self.assertIn('归属表格', text)
        idx = text.index('归属表格')
        self.assertIn('月', text[idx:idx + 12])

    def test_pay_date_can_be_prefilled(self):
        doc, _, _ = self.build([self.make_entry('A')], pay_date_text='2026-10-07')
        self.assertIn('2026-10-07', self.back_text(doc))

    def test_purpose_and_group_come_from_the_item(self):
        doc, _, _ = self.build([self.make_entry('A', purpose='高压保护', group_name='动力组')])
        text = self.back_text(doc)
        self.assertIn('高压保护', text)
        self.assertIn('动力组', text)

    def test_group_falls_back_to_options(self):
        doc, _, _ = self.build([self.make_entry('A', group_name='')], group='电气组')
        self.assertIn('电气组', self.back_text(doc))

    def test_each_back_block_gets_its_own_item(self):
        doc, _, _ = self.build([self.make_entry('充电机'), self.make_entry('绝缘手套')])
        text = self.back_text(doc)
        self.assertIn('充电机', text)
        self.assertIn('绝缘手套', text)

    def test_odd_group_back_has_only_one_block(self):
        """3 张每页 2 → 第 2 组的背面只有 C 的信息，没有第三个块。"""
        doc, _, _ = self.build([self.make_entry('A'), self.make_entry('B'), self.make_entry('C')])
        back2 = doc[3].get_text()
        self.assertIn('C', back2)
        self.assertNotIn('物品名称：A', back2)
        self.assertNotIn('物品名称：B', back2)

    def test_title_is_printed_when_given(self):
        doc, _, _ = self.build([self.make_entry('A')], title='电气组 8月 发票背面')
        self.assertIn('电气组 8月 发票背面', self.back_text(doc))

    def test_no_title_by_default(self):
        doc, _, _ = self.build([self.make_entry('A')])
        self.assertNotIn('发票背面', self.back_text(doc))


class TestInvoiceMeta(PdfCase):

    def test_meta_line_shows_what_is_known(self):
        entry = self.make_entry('PCB物料', paid=157.92)
        entry['invoice'] = {'invoice_no': '2431200000001234', 'invoice_date': '2026-08-12',
                            'amount': 157.92, 'seller': '某某电子'}
        doc, _, _ = self.build([entry])
        text = self.back_text(doc)
        self.assertIn('2431200000001234', text)
        self.assertIn('2026-08-12', text)
        self.assertIn('157.92', text)
        self.assertIn('某某电子', text)

    def test_meta_falls_back_to_paid_amount(self):
        entry = self.make_entry('绝缘手套', paid=500)
        doc, _, _ = self.build([entry])
        self.assertIn('价税合计 ¥500', self.back_text(doc))

    def test_meta_can_be_turned_off(self):
        entry = self.make_entry('A', paid=500)
        doc, _, _ = self.build([entry], show_invoice_meta=False)
        self.assertNotIn('价税合计', self.back_text(doc))


class TestFrontSide(PdfCase):

    def test_front_embeds_the_invoice_page(self):
        doc, _, _ = self.build([self.make_entry('A')])
        # 正面应当引用了源 PDF 的页面（xobject），文本层里会出现我们写进去的 INVOICE
        self.assertIn('INVOICE', doc[0].get_text())

    def test_each_front_group_holds_its_own_invoices(self):
        a = self.make_entry('A', filename='a.pdf')
        b = self.make_entry('B', filename='b.pdf')
        doc, _, _ = self.build([a, b])
        front = doc[0].get_text()
        self.assertEqual(front.count('INVOICE'), 2, '一页应当放两张')

    def test_unreadable_pdf_becomes_a_placeholder_not_a_crash(self):
        bad = self.root / 'bad.pdf'
        bad.write_bytes(b'not really a pdf')
        entry = self.make_entry('A', filename='ok.pdf')
        entry['path'] = bad
        doc, _, stats = self.build([entry])
        self.assertEqual(stats['invoices'], 1)
        self.assertIn('无法打开', doc[0].get_text())

    def test_image_invoice_is_placed(self):
        """图片发票（扫描件/截图）也能排版 —— 用 PyMuPDF 直接造一张 PNG，不依赖 Pillow。"""
        img = self.root / 'invoice.png'
        src = fitz.open()
        pg = src.new_page(width=300, height=200)
        pg.insert_text((10, 30), 'IMG', fontsize=12)
        pg.get_pixmap().save(str(img))
        src.close()
        self.assertTrue(img.exists())

        entry = self.make_entry('A', filename='ok.pdf')
        entry['path'] = img
        doc, _, stats = self.build([entry])
        self.assertEqual(stats['invoices'], 1)
        self.assertEqual(doc.page_count, 2)


class TestOptions(PdfCase):

    def test_empty_entries_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build([])

    def test_creates_missing_output_directory(self):
        entries = [self.make_entry('A')]
        out = self.root / '深层' / '目录' / '合并.pdf'
        path, _ = invoicepdf.build_merged_pdf(entries, out, MergeOptions())
        self.assertTrue(path.exists())

    def test_per_page_is_clamped(self):
        entries = [self.make_entry(str(i)) for i in range(4)]
        _, _, stats = self.build(entries, per_page=99)
        self.assertLessEqual(stats['per_page'], 4)


class TestBackFieldsManyItems(unittest.TestCase):
    """一张票覆盖好几条时，背面要把每一样都列出来。

    淘宝一个订单买几样、商家只开一张票，只印主条目那一样就对不上账。
    """

    def _opts(self):
        return MergeOptions()

    def test_a_single_item_stays_on_one_line(self):
        f = invoicepdf.back_fields({'name': '电容 22uF'}, self._opts())
        self.assertEqual(f[0], ('物品名称', '电容 22uF', 0.0))
        self.assertNotIn('\n', f[0][1])

    def test_extra_names_go_on_their_own_lines(self):
        f = invoicepdf.back_fields({'name': '电容 22uF'}, self._opts(),
                                   ['AO3400A', '电阻包'])
        self.assertEqual(f[0][1], '电容 22uF\nAO3400A\n电阻包')

    def test_the_same_name_is_not_repeated(self):
        f = invoicepdf.back_fields({'name': '电容 22uF'}, self._opts(),
                                   ['电容 22uF', 'AO3400A'])
        self.assertEqual(f[0][1], '电容 22uF\nAO3400A')

    def test_blank_extras_are_skipped(self):
        f = invoicepdf.back_fields({'name': '电容 22uF'}, self._opts(),
                                   ['', None, '   ', 'AO3400A'])
        self.assertEqual(f[0][1], '电容 22uF\nAO3400A')

    def test_none_means_business_as_usual(self):
        f = invoicepdf.back_fields({'name': '电容 22uF'}, self._opts(), None)
        self.assertEqual(f[0][1], '电容 22uF')

    def test_the_other_four_fields_are_untouched(self):
        f = invoicepdf.back_fields({'name': '电容', 'purpose': '高压保护'},
                                   self._opts(), ['另一件'])
        self.assertEqual([x[0] for x in f], BACK_LABELS)
        self.assertEqual(f[1][1], '高压保护')


class TestReadAmount(unittest.TestCase):
    """从发票 PDF 的文字层读「价税合计」，用来预填实付金额。

    读不到就返回 None —— 图片票没有文字层，宁可留空手填，也别瞎猜一个数。
    """

    def _pdf(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / 'x.pdf'
        doc = fitz.open()
        page = doc.new_page()
        # fitz 自带中文字体，不然中文会写成方块，读出来是空的
        page.insert_text((60, 100), text, fontname='china-s', fontsize=12)
        doc.save(str(p))
        doc.close()
        return p

    def test_reads_the_standard_field(self):
        self.assertEqual(
            invoicepdf.read_amount(self._pdf('价税合计（小写）¥16.00')), 16.0)

    def test_reads_with_fullwidth_parens_variant(self):
        self.assertEqual(
            invoicepdf.read_amount(self._pdf('（小写）￥1234.56')), 1234.56)

    def test_falls_back_to_the_largest_yen_number(self):
        """认不出标准字段时，取带 ¥ 的最大数（发票上通常就是价税合计）。"""
        got = invoicepdf.read_amount(self._pdf('金额 ¥15.84  税额 ¥0.16  总计 ¥16.00'))
        self.assertEqual(got, 16.0)

    def test_returns_none_for_a_pdf_without_a_text_layer(self):
        """扫描件/图片转的票读不出文字 —— 必须返回 None，不能编一个数。"""
        d = tempfile.mkdtemp()
        p = Path(d) / 'blank.pdf'
        doc = fitz.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        self.assertIsNone(invoicepdf.read_amount(p))

    def test_returns_none_for_a_broken_file(self):
        d = tempfile.mkdtemp()
        p = Path(d) / 'not.pdf'
        p.write_bytes(b'this is not a pdf at all')
        self.assertIsNone(invoicepdf.read_amount(p))

    def test_integer_amount_still_works(self):
        self.assertEqual(
            invoicepdf.read_amount(self._pdf('价税合计（小写）¥192')), 192.0)


    def test_reads_every_detail_line_of_a_multi_item_invoice(self):
        """一票多单：明细有几行就读几行，不能只给一个总额。

        坐标照抄真票：列头「金」「额」在 y≈151，明细行 y≈161 / 200，
        合计行 y≈259（也落在同一列，必须排除）。
        """
        d = tempfile.mkdtemp()
        p = Path(d) / 'multi.pdf'
        doc = fitz.open()
        page = doc.new_page(width=595, height=397)
        page.insert_text((407, 151), '金', fontname='china-s', fontsize=8)
        page.insert_text((428, 151), '额', fontname='china-s', fontsize=8)
        page.insert_text((407, 161), '21.78', fontsize=8)
        page.insert_text((407, 200), '10.89', fontsize=8)
        page.insert_text((402, 259), '¥32.67', fontsize=8)   # 合计行
        doc.save(str(p))
        doc.close()

        self.assertEqual([x['amount'] for x in invoicepdf.read_lines(p)],
                         [21.78, 10.89])
        self.assertEqual(invoicepdf.read_lines_total(p), 32.67)

    def test_a_single_item_invoice_gives_one_line(self):
        d = tempfile.mkdtemp()
        p = Path(d) / 'one.pdf'
        doc = fitz.open()
        page = doc.new_page(width=595, height=397)
        page.insert_text((407, 151), '金', fontname='china-s', fontsize=8)
        page.insert_text((428, 151), '额', fontname='china-s', fontsize=8)
        page.insert_text((407, 161), '204.95', fontsize=8)
        doc.save(str(p))
        doc.close()
        self.assertEqual([x['amount'] for x in invoicepdf.read_lines(p)], [204.95])

    def test_a_picture_invoice_gives_nothing(self):
        """图片票没有文字层 —— 返回空，别编一个数出来。"""
        d = tempfile.mkdtemp()
        p = Path(d) / 'blank.pdf'
        doc = fitz.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        self.assertEqual(invoicepdf.read_lines(p), [])
        self.assertIsNone(invoicepdf.read_lines_total(p))

    def test_numbers_outside_the_amount_column_are_ignored(self):
        """单价、数量、税额都在别的列 —— 不能被当成金额。"""
        d = tempfile.mkdtemp()
        p = Path(d) / 'cols.pdf'
        doc = fitz.open()
        page = doc.new_page(width=595, height=397)
        page.insert_text((407, 151), '金', fontname='china-s', fontsize=8)
        page.insert_text((428, 151), '额', fontname='china-s', fontsize=8)
        page.insert_text((407, 161), '21.78', fontsize=8)     # 金额列 ✓
        page.insert_text((297, 161), '10.89', fontsize=8)     # 单价列 ✗
        page.insert_text((286, 161), '2', fontsize=8)         # 数量列 ✗
        page.insert_text((565, 161), '0.22', fontsize=8)      # 税额列 ✗
        doc.save(str(p))
        doc.close()
        self.assertEqual([x['amount'] for x in invoicepdf.read_lines(p)], [21.78])


    def test_each_line_carries_its_tax_and_total(self):
        """每行含税 = 金额 + 税额 ——「实付金额」要填含税的。

        用真票的坐标：金额列 x≈407、税额列 x≈560，两行明细。
        """
        d = tempfile.mkdtemp()
        p = Path(d) / 'm.pdf'
        doc = fitz.open()
        page = doc.new_page(width=595, height=397)
        page.insert_text((407, 151), '金', fontname='china-s', fontsize=8)
        page.insert_text((428, 151), '额', fontname='china-s', fontsize=8)
        page.insert_text((560, 151), '税', fontname='china-s', fontsize=8)
        page.insert_text((578, 151), '额', fontname='china-s', fontsize=8)
        page.insert_text((407, 161), '21.78', fontsize=8)
        page.insert_text((560, 161), '0.22', fontsize=8)
        page.insert_text((407, 200), '10.89', fontsize=8)
        page.insert_text((560, 200), '0.11', fontsize=8)
        doc.save(str(p))
        doc.close()

        got = invoicepdf.read_lines(p)
        self.assertEqual([x['amount'] for x in got], [21.78, 10.89])
        self.assertEqual([x['tax'] for x in got], [0.22, 0.11])
        self.assertEqual([x['total'] for x in got], [22.0, 11.0])
        # 各行含税之和要跟票面的价税合计对得上（33.00）
        self.assertEqual(invoicepdf.read_lines_total(p), 33.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
