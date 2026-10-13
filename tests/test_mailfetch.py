# -*- coding: utf-8 -*-
"""收信落盘与去重（客户端注入，不碰真实邮箱）。"""
import unittest

from app import config
from app.core import mailbox, mailfetch, repo
from tests.support import TempDataTest
from tests.test_mailbox import PDF, FakeClient, build_mail


class FetchCase(TempDataTest):

    def cfg(self, **over):
        base = {'host': 'imap.example.com', 'user': 'a@example.com', 'password': 'x',
                'days': 0, 'max_mails': 50}
        base.update(over)
        return mailbox.MailConfig(**base)

    def messages(self, n=1, **kw):
        return [('%d' % (i + 1), build_mail(**kw).as_bytes()) for i in range(n)]


class TestCollect(FetchCase):

    def test_saves_attachment_and_records_it(self):
        r = mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        self.assertTrue(r['ok'], r['message'])
        self.assertEqual(r['new'], 1)
        self.assertEqual(r['saved'][0]['filename'], 'invoice.pdf')

        rows = repo.list_mail_attachments(status=repo.MAIL_PENDING)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['orig_name'], 'invoice.pdf')
        self.assertEqual(row['subject'], '电子发票')
        self.assertEqual(row['sender'], 'shop@example.com')
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['size'], len(PDF))
        self.assertEqual(row['sha1'], mailbox.sha1_of(PDF))

        path = config.DATA_DIR / row['rel_path']
        self.assertTrue(path.exists(), '附件要真的落盘')
        self.assertEqual(path.read_bytes(), PDF)
        self.assertTrue(row['rel_path'].startswith('mail_inbox/'), row['rel_path'])

    def test_second_run_skips_the_same_mail(self):
        mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        again = mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        self.assertEqual(again['new'], 0)
        self.assertEqual(again['skipped'][0]['uid'], '1')
        self.assertIn('已经收过', again['skipped'][0]['reason'])
        self.assertEqual(len(repo.list_mail_attachments()), 1)

    def test_same_attachment_in_two_mails_is_deduplicated(self):
        """同一张发票被转发到两封邮件里 —— 只留一份。"""
        mails = [('1', build_mail(subject='发票A').as_bytes()),
                 ('2', build_mail(subject='发票B', filename='copy.pdf').as_bytes())]
        r = mailfetch.collect(self.cfg(), client=FakeClient(mails))
        self.assertEqual(r['new'], 1)
        self.assertEqual(len(r['duplicates']), 1)
        self.assertEqual(len(repo.list_mail_attachments()), 1)
        self.assertEqual(len(list(config.MAIL_INBOX_DIR.iterdir())), 1)

    def test_two_different_attachments_are_both_saved(self):
        mails = [('1', build_mail(subject='发票A').as_bytes()),
                 ('2', build_mail(subject='发票B', filename='b.pdf',
                                  data=PDF + b'2').as_bytes())]
        r = mailfetch.collect(self.cfg(), client=FakeClient(mails))
        self.assertEqual(r['new'], 2)
        self.assertEqual(len(list(config.MAIL_INBOX_DIR.iterdir())), 2)

    def test_not_configured_is_reported(self):
        r = mailfetch.collect(mailbox.MailConfig(), client=FakeClient([]))
        self.assertFalse(r['ok'])
        self.assertIn('还没配置', r['message'])

    def test_client_failure_is_reported(self):
        class Boom(FakeClient):
            def search_uids(self, criteria):
                raise RuntimeError('IMAP 断了')

        r = mailfetch.collect(self.cfg(), client=Boom([]))
        self.assertFalse(r['ok'])
        self.assertIn('IMAP 断了', r['message'])

    def test_bad_filename_is_sanitised(self):
        mails = [('1', build_mail(subject='发票', filename='a/b:c*.pdf').as_bytes())]
        r = mailfetch.collect(self.cfg(), client=FakeClient(mails))
        self.assertEqual(r['new'], 1)
        row = repo.list_mail_attachments()[0]
        for ch in ':*?':
            self.assertNotIn(ch, row['stored_name'], row['stored_name'])
        self.assertTrue((config.DATA_DIR / row['rel_path']).exists())

    def test_mail_without_invoice_is_skipped(self):
        mails = [('1', build_mail(subject='闲聊', filename='x.exe', data=b'MZ').as_bytes())]
        r = mailfetch.collect(self.cfg(), client=FakeClient(mails))
        self.assertEqual(r['new'], 0)
        self.assertEqual(repo.list_mail_attachments(), [])

    def test_errors_do_not_abort_the_rest(self):
        mails = [('1', build_mail(subject='好的').as_bytes()),
                 ('2', build_mail(subject='坏的', filename='b.pdf').as_bytes())]
        r = mailfetch.collect(self.cfg(), client=FakeClient(mails, fail_uids=['2']))
        self.assertTrue(r['ok'])
        self.assertEqual(r['new'], 1)
        self.assertTrue(any('2' in e for e in r['errors']))


class TestCounts(FetchCase):

    def test_counts_by_status(self):
        mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        c = repo.mail_counts()
        self.assertEqual(c.get('pending'), 1)
        self.assertEqual(c['total'], 1)

    def test_empty(self):
        self.assertEqual(repo.mail_counts()['total'], 0)


class TestDelete(FetchCase):

    def test_delete_removes_record_and_file(self):
        mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        mid = repo.list_mail_attachments()[0]['id']
        path = config.DATA_DIR / repo.get_mail_attachment(mid)['rel_path']
        self.assertTrue(path.exists())
        self.assertTrue(repo.delete_mail_attachment(mid))
        self.assertFalse(path.exists())
        self.assertEqual(repo.list_mail_attachments(), [])

    def test_delete_missing_returns_false(self):
        self.assertFalse(repo.delete_mail_attachment(999))

    def test_seen_uids_and_sha1_are_exposed(self):
        mailfetch.collect(self.cfg(), client=FakeClient(self.messages()))
        self.assertEqual(repo.mail_seen_uids(), {'1'})
        self.assertEqual(repo.mail_known_sha1(), {mailbox.sha1_of(PDF)})


class TestReport(FetchCase):

    def test_report_mentions_saved_and_duplicates(self):
        mails = [('1', build_mail().as_bytes()),
                 ('2', build_mail(filename='copy.pdf').as_bytes())]
        text = '\n'.join(mailfetch.report_lines(
            mailfetch.collect(self.cfg(), client=FakeClient(mails))))
        self.assertIn('新收到 1 张', text)
        self.assertIn('重复', text)

    def test_report_on_failure_is_not_empty(self):
        text = mailfetch.report_lines(
            mailfetch.collect(mailbox.MailConfig(), client=FakeClient([])))
        self.assertTrue(text and text[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
