# -*- coding: utf-8 -*-
"""并发场景：后台自动挂载 / 人工点「挂上」/ 收信，撞在一起会怎样。

这些都是**静默出错**的类型 —— 不报错、界面看着正常，
但数据库里多出一条发票记录或多一封邮件，事后极难发现。
"""
import threading
import unittest

from app import config
from app.core import db, mailfetch, mailbox, repo
from tests.support import TempDataTest
from tests.test_mailbox import FakeClient, build_mail


def run_together(fn, n=2):
    """让 n 个线程尽量同时执行 fn，返回每次的结果（'ok' 或异常）。"""
    out = []
    lock = threading.Lock()

    def worker():
        try:
            fn()
            with lock:
                out.append('ok')
        except Exception as exc:
            with lock:
                out.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


class AttachRaceCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.rid = repo.create_round('十月采购')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': '电容', 'qty': 1, 'unit': '个', 'price': 22,
            'purpose': 'x', 'applicant': '张三', 'paid': 22})
        self.mail_id = self.seed_pending()

    def seed_pending(self, subject='电子发票 ¥22.00', filename='a.pdf',
                     data=b'%PDF-1.4 a'):
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0, auto_attach=False)
        r = mailfetch.collect(cfg, client=FakeClient(
            [('1', build_mail(subject=subject, filename=filename,
                              data=data).as_bytes())]))
        assert r['new'] == 1, r
        return r['saved'][0]['id']


class TestTwoPeopleAttachAtOnce(AttachRaceCase):
    """两个人同时点「挂上」同一张票 —— 只能有一个成功。

    修之前：「是否已挂」的检查在事务**外面**，两边都能通过，
    于是同一个文件被登记两次 → 合并打印把同一张票印两遍、报销算两次。
    """

    def test_only_one_attach_succeeds(self):
        out = run_together(lambda: mailfetch.attach_to_item(self.mail_id, self.iid), 2)
        self.assertEqual(out.count('ok'), 1, out)

    def test_only_one_invoice_row_is_created(self):
        run_together(lambda: mailfetch.attach_to_item(self.mail_id, self.iid), 2)
        self.assertEqual(len(repo.list_invoices()), 1)
        self.assertEqual(len(repo.invoices_of_item(self.iid)), 1)

    def test_the_loser_gets_a_readable_error(self):
        out = run_together(lambda: mailfetch.attach_to_item(self.mail_id, self.iid), 2)
        errs = [x for x in out if x != 'ok']
        self.assertEqual(len(errs), 1)
        self.assertTrue('已经挂' in str(errs[0]), errs)


class TestMultiAttachRace(AttachRaceCase):
    """一票多条那条路径也要有同样的保护。"""

    def setUp(self):
        super().setUp()
        self.iid2 = repo.add_item(self.rid, self.mid, {
            'name': '电阻', 'qty': 1, 'unit': '个', 'price': 10,
            'purpose': 'x', 'applicant': '张三', 'paid': 10})

    def test_only_one_multi_attach_succeeds(self):
        out = run_together(
            lambda: mailfetch.attach_to_items(self.mail_id, [self.iid, self.iid2]), 2)
        self.assertEqual(out.count('ok'), 1, out)
        self.assertEqual(len(repo.list_invoices()), 1)


class TestCollectRace(AttachRaceCase):
    """后台收信和人工点「立即收取」可能同时发生。

    修之前：`collect()` 没有任何互斥，两边会把同一封邮件各存一份 ——
    两条记录共用同一个 `rel_path`，其中一条被忽略/挂载后文件就没了，
    另一条点开直接 404。
    """

    def test_two_collects_do_not_double_save(self):
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0, auto_attach=False)
        mails = [('9', build_mail(subject='电子发票 ¥77.00', filename='race.pdf',
                                  data=b'%PDF-1.4 race').as_bytes())]
        run_together(lambda: mailfetch.collect(cfg, client=FakeClient(mails)), 2)
        rows = [r for r in repo.list_mail_attachments() if r['orig_name'] == 'race.pdf']
        self.assertEqual(len(rows), 1, '同一封邮件只能入库一次')


if __name__ == '__main__':
    unittest.main(verbosity=2)
