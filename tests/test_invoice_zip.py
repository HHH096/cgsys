# -*- coding: utf-8 -*-
"""把一轮的发票打包成 zip 下载。

发票原件除了「合并打印」，有时还得单独交出去（给财务、给商家、存档、发群里）——
一张张点「存」太费手。zip 里就是归档时的文件名 `DQ-品项-金额.pdf`，
而且**按发票记录打包**：一票多条时那张票只出现一次，不会重复。
"""
import io
import unittest
import zipfile
from pathlib import Path

from app import config
from app.core import repo
from tests.support import EntryCase


class TestInvoiceZip(EntryCase):

    def setUp(self):
        super().setUp()
        self.a = self._item('电容', 22)
        self.b = self._item('电阻', 11)

    def _item(self, name, price):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def _file(self, name, data=None):
        """把归档文件真的写出来 —— 接口会检查它在不在。"""
        p = Path(config.INVOICE_DIR) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if data is not None else
                      ('%PDF-1.4 fake ' + name).encode('utf-8'))
        return 'invoices/' + name

    def _ticket(self, item_id, name, data=None):
        rel = self._file(name, data)
        return repo.add_invoice(item_id, self.rid, name, name, rel, 100)

    def get_zip(self, client=None):
        return (client or self.purchase).get('/api/rounds/%d/invoices.zip' % self.rid)

    def names(self, resp):
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        try:
            return sorted(zf.namelist())
        finally:
            zf.close()

    # ------------------------------------------------------------ 正常路径

    def test_it_zips_every_invoice_of_the_round(self):
        self._ticket(self.a, 'DQ-电容-22.pdf')
        self._ticket(self.b, 'DQ-电阻-11.pdf')
        r = self.get_zip()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('zip', r.headers['content-type'])
        self.assertEqual(self.names(r), ['DQ-电容-22.pdf', 'DQ-电阻-11.pdf'])

    def test_the_file_content_comes_through(self):
        self._ticket(self.a, 'DQ-电容-22.pdf')
        zf = zipfile.ZipFile(io.BytesIO(self.get_zip().content))
        try:
            self.assertEqual(zf.read('DQ-电容-22.pdf'),
                             '%PDF-1.4 fake DQ-电容-22.pdf'.encode('utf-8'))
        finally:
            zf.close()

    def test_one_invoice_covering_several_items_appears_once(self):
        """一票多条：那张票只该出现一次（zip 按发票记录打，不按条目）。"""
        inv = self._ticket(self.a, 'DQ-电容-22.pdf')
        repo.link_invoice(inv, self.b)
        self.assertEqual(self.names(self.get_zip()), ['DQ-电容-22.pdf'])

    def test_another_round_is_not_included(self):
        other = repo.create_round('八月采购')
        iid = repo.add_item(other, self.alice, {
            'name': '别的', 'qty': 1, 'unit': '个', 'price': 1,
            'purpose': 'x', 'applicant': 'x'})
        rel = self._file('DQ-别的-1.pdf')
        repo.add_invoice(iid, other, 'DQ-别的-1.pdf', 'DQ-别的-1.pdf', rel, 10)
        self._ticket(self.a, 'DQ-电容-22.pdf')
        self.assertEqual(self.names(self.get_zip()), ['DQ-电容-22.pdf'])

    def test_duplicate_names_get_a_suffix(self):
        """理论上不会撞名；真撞了也不能让后一个把前一个顶掉。"""
        self._ticket(self.a, '同名.pdf')
        self._ticket(self.b, '同名.pdf', data=b'second')   # 记录、stored_name 同名
        self.assertEqual(set(self.names(self.get_zip())),
                         {'同名.pdf', '同名-2.pdf'})

    # ------------------------------------------------------------ 该报错的时候

    def test_an_empty_round_is_a_bad_request(self):
        r = self.get_zip()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('还没有发票', r.text)

    def test_a_missing_file_is_reported(self):
        """记录在、归档文件没了 —— 跟合并打印一个规矩：报出来，别装糊涂。"""
        repo.add_invoice(self.a, self.rid, '丢了.pdf', '丢了.pdf',
                         'invoices/丢了.pdf', 10)
        r = self.get_zip()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('丢了.pdf', r.text)

    def test_a_missing_round_is_a_404(self):
        self.assertEqual(
            self.purchase.get('/api/rounds/999999/invoices.zip').status_code, 404)

    # ------------------------------------------------------------ 谁能下

    def test_a_member_cannot_download_the_zip(self):
        """打包下载是采购的事；组员要哪张，点条目行里的「存」。"""
        self._ticket(self.a, 'DQ-电容-22.pdf')
        r = self.alice_c.get('/api/rounds/%d/invoices.zip' % self.rid)
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_an_anonymous_caller_is_rejected(self):
        self._ticket(self.a, 'DQ-电容-22.pdf')
        r = self.anon.get('/api/rounds/%d/invoices.zip' % self.rid)
        self.assertIn(r.status_code, (401, 403), r.text)


class TestUniqueZipName(unittest.TestCase):
    """zip 里的重名处理（纯函数，单独钉一下）。"""

    def name(self, *names):
        from app.server.api import _unique_zip_name
        used = set()
        return [_unique_zip_name(n, used) for n in names]

    def test_the_first_one_keeps_its_name(self):
        self.assertEqual(self.name('a.pdf', 'b.pdf'), ['a.pdf', 'b.pdf'])

    def test_a_clash_gets_numbered(self):
        self.assertEqual(self.name('a.pdf', 'a.pdf', 'a.pdf'),
                         ['a.pdf', 'a-2.pdf', 'a-3.pdf'])

    def test_a_name_without_extension_still_works(self):
        self.assertEqual(self.name('票', '票'), ['票', '票-2'])

    def test_an_empty_name_gets_a_default(self):
        self.assertEqual(self.name(''), ['invoice.pdf'])


class TestFrontendZipWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')

    def test_the_button_is_there_and_wired(self):
        self.assertIn('id="invoicesZip"', self.html)
        self.assertIn("$('invoicesZip').addEventListener('click', invoiceZip)", self.js)

    def test_it_hits_the_zip_endpoint(self):
        seg = self.js.split('function invoiceZip(')[1].split('\nfunction ')[0]
        self.assertIn('/invoices.zip', seg)

    def test_it_says_something_when_there_is_no_invoice(self):
        """没票就别跳过去让人看一页 JSON 报错。"""
        seg = self.js.split('function invoiceZip(')[1].split('\nfunction ')[0]
        self.assertIn('还没有发票', seg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
