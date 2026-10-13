# -*- coding: utf-8 -*-
"""待归档列表、建议匹配、一键挂到条目。"""
import unittest

from app import config
from app.core import mailbox, mailfetch, repo
from tests.support import TempDataTest
from tests.test_mailbox import FakeClient, build_mail


class AttachCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '13800000000')
        self.rid = repo.create_round('八月采购')

    def item(self, name, price, paid=None, url=''):
        data = {'name': name, 'qty': 1, 'unit': '个', 'price': price, 'purpose': '用',
                'applicant': '张三', 'group_name': '电气组', 'url': url}
        if paid is not None:
            data['paid'] = paid
        return repo.get_item(repo.add_item(self.rid, self.mid, data))

    def collect(self, **over):
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0)
        kw = {'subject': '电子发票', 'filename': 'invoice.pdf'}
        kw.update(over)
        r = mailfetch.collect(cfg, client=FakeClient([('1', build_mail(**kw).as_bytes())]))
        self.assertTrue(r['ok'], r['message'])
        self.assertEqual(r['new'], 1, '前置条件：应当收到 1 张')
        return r

    def pending(self):
        rows = repo.list_mail_attachments(status=repo.MAIL_PENDING)
        self.assertTrue(rows, '前置条件：应当有待归档记录')
        return rows[0]


class TestExtractAmounts(unittest.TestCase):

    def test_yen_prefixed(self):
        self.assertEqual(mailfetch.extract_amounts('发票 ¥6200.00'), [6200.0])
        self.assertEqual(mailfetch.extract_amounts('￥23'), [23.0])

    def test_two_decimals_without_symbol(self):
        self.assertEqual(mailfetch.extract_amounts('invoice total 157.92 yuan'), [157.92])

    def test_plain_integers_are_ignored(self):
        """数量、型号、订单号都是整数，不能当成金额。"""
        self.assertEqual(mailfetch.extract_amounts('型号 9011 数量 2 订单 12345'), [])

    def test_empty(self):
        self.assertEqual(mailfetch.extract_amounts(''), [])
        self.assertEqual(mailfetch.extract_amounts(None), [])


class TestSuggest(AttachCase):

    def test_amount_match_is_the_strongest_hint(self):
        self.item('充电机', 6200, paid=6200)
        self.item('绝缘手套', 250, paid=500)
        self.collect(subject='电子发票 ¥6200.00')
        got = mailfetch.suggest_items(self.pending(), repo.list_items(self.rid))
        self.assertTrue(got)
        self.assertEqual(got[0]['name'], '充电机')
        self.assertIn('金额', got[0]['reason'])

    def test_item_name_in_subject_is_a_hint(self):
        self.item('充电机', 6200)
        self.item('绝缘手套', 250)
        self.collect(subject='电子发票 充电机')
        got = mailfetch.suggest_items(self.pending(), repo.list_items(self.rid))
        self.assertEqual(got[0]['name'], '充电机')

    def test_supplier_name_is_a_hint(self):
        self.item('充电机', 6200, url='浩泰科技')
        self.item('绝缘手套', 250, url='速迫')
        self.collect(subject='速迫 的发货发票')
        got = mailfetch.suggest_items(self.pending(), repo.list_items(self.rid))
        self.assertEqual(got[0]['name'], '绝缘手套')

    def test_no_clue_gives_nothing(self):
        self.item('充电机', 6200)
        self.collect(subject='电子发票')
        self.assertEqual(mailfetch.suggest_items(self.pending(), repo.list_items(self.rid)), [])

    def test_limit_is_respected(self):
        for _ in range(5):
            self.item('充电机', 6200)
        self.collect(subject='电子发票 ¥6200.00')
        got = mailfetch.suggest_items(self.pending(), repo.list_items(self.rid), limit=2)
        self.assertEqual(len(got), 2)

    def test_amount_from_filename_also_works(self):
        self.item('充电机', 6200, paid=6200)
        self.collect(subject='电子发票', filename='发票-6200.00.pdf')
        got = mailfetch.suggest_items(self.pending(), repo.list_items(self.rid))
        self.assertEqual(got[0]['name'], '充电机')


class TestAttach(AttachCase):

    def test_attach_archives_and_updates_everything(self):
        it = self.item('充电机', 6200, paid=6200)
        self.collect()
        row = self.pending()
        before = config.DATA_DIR / row['rel_path']
        self.assertTrue(before.exists())

        res = mailfetch.attach_to_item(row['id'], it['id'])

        self.assertEqual(res['stored_name'], 'DQ-充电机-6200.pdf')
        self.assertEqual(res['item']['invoice_status'], '已收到')
        self.assertEqual(res['item']['invoice'], '有')

        archived = config.DATA_DIR / res['invoice']['rel_path']
        self.assertTrue(archived.exists(), '归档文件应当存在')
        self.assertEqual(archived.read_bytes(), before.read_bytes() if before.exists() else archived.read_bytes())
        self.assertTrue(res['invoice']['rel_path'].startswith('invoices/八月采购/'))
        self.assertFalse(before.exists(), '挂好之后待归档区那份要清掉，别留两份')

        after = repo.get_mail_attachment(row['id'])
        self.assertEqual(after['status'], repo.MAIL_ATTACHED)
        self.assertEqual(after['item_id'], it['id'])
        self.assertEqual(after['invoice_id'], res['invoice_id'])
        self.assertEqual(repo.invoice_counts(self.rid), {it['id']: 1})

    def test_attach_twice_is_rejected(self):
        it = self.item('充电机', 6200, paid=6200)
        self.collect()
        row = self.pending()
        mailfetch.attach_to_item(row['id'], it['id'])
        with self.assertRaises(ValueError):
            mailfetch.attach_to_item(row['id'], it['id'])

    def test_attach_to_missing_item_is_rejected(self):
        self.item('充电机', 6200)
        self.collect()
        with self.assertRaises(ValueError):
            mailfetch.attach_to_item(self.pending()['id'], 9999)

    def test_attach_missing_record_is_rejected(self):
        with self.assertRaises(ValueError):
            mailfetch.attach_to_item(9999, 1)

    def test_attach_when_file_vanished_is_reported(self):
        it = self.item('充电机', 6200, paid=6200)
        self.collect()
        row = self.pending()
        (config.DATA_DIR / row['rel_path']).unlink()
        with self.assertRaises(ValueError) as cm:
            mailfetch.attach_to_item(row['id'], it['id'])
        self.assertIn('找不到', str(cm.exception))

    def test_attach_takes_the_item_off_the_missing_invoice_list(self):
        it = self.item('充电机', 6200, paid=6200)
        self.assertEqual(len(repo.items_pending_invoice(self.rid)), 1)
        self.collect()
        mailfetch.attach_to_item(self.pending()['id'], it['id'])
        self.assertEqual(repo.items_pending_invoice(self.rid), [])


class TestPendingList(AttachCase):

    def test_pending_carries_suggestions(self):
        self.item('充电机', 6200, paid=6200)
        self.collect(subject='电子发票 ¥6200.00')
        rows = mailfetch.list_pending(self.rid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], repo.MAIL_PENDING)
        self.assertTrue(rows[0]['suggestions'])
        self.assertEqual(rows[0]['suggestions'][0]['name'], '充电机')
        self.assertIn('reason', rows[0]['suggestions'][0])

    def test_attached_rows_have_no_suggestions_left(self):
        it = self.item('充电机', 6200, paid=6200)
        self.collect(subject='电子发票 ¥6200.00')
        mailfetch.attach_to_item(mailfetch.list_pending(self.rid)[0]['id'], it['id'])
        rows = mailfetch.list_pending(self.rid)
        self.assertEqual(rows[0]['status'], repo.MAIL_ATTACHED)
        self.assertEqual(rows[0]['suggestions'], [])

    def test_without_round_it_still_lists_but_without_suggestions(self):
        self.item('充电机', 6200, paid=6200)
        self.collect(subject='电子发票 ¥6200.00')
        rows = mailfetch.list_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['suggestions'], [])

    def test_empty_when_nothing_collected(self):
        self.assertEqual(mailfetch.list_pending(self.rid), [])


class TestIgnore(AttachCase):

    def test_ignore_removes_record_and_file(self):
        self.item('充电机', 6200)
        self.collect()
        row = self.pending()
        path = config.DATA_DIR / row['rel_path']
        self.assertTrue(path.exists())
        self.assertTrue(mailfetch.ignore(row['id']))
        self.assertFalse(path.exists())
        self.assertEqual(repo.list_mail_attachments(), [])

    def test_ignore_missing_returns_false(self):
        self.assertFalse(mailfetch.ignore(9999))


if __name__ == '__main__':
    unittest.main(verbosity=2)
