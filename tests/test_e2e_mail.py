# -*- coding: utf-8 -*-
"""邮箱收发票的端到端：收信 → 待归档 → 挂载 → 归档 → 导出。

只把最底层的网络连接换成假的（模仿 imaplib 的返回结构，不起真实邮件服务器），
其余全部走真实代码：**HTTP 接口、ImapClient 的封装与解析、附件解析、落盘去重、
归档登记、导出**。这样才验证得到「响应结构被我解析对了」这一点。
"""
import unittest
from email.message import EmailMessage
from unittest import mock

from app import config
from app.core import mailbox
from tests.test_e2e import LiveServerCase

PDF_A = b'%PDF-1.4 invoice-a'
PDF_B = b'%PDF-1.4 invoice-b'


def make_mail(subject, filename, data):
    m = EmailMessage()
    m['Subject'] = subject
    m['From'] = 'shop@example.com'
    m['Date'] = 'Mon, 06 Oct 2026 10:00:00 +0800'
    m.set_content('发票见附件')
    m.add_attachment(data, maintype='application', subtype='pdf', filename=filename)
    return m.as_bytes()


class FakeImapConn:
    """模仿 imaplib 的返回结构，好让 ImapClient 的真解析逻辑也跑一遍。"""

    def __init__(self, mails):
        self.mails = list(mails)          # [(uid, raw_bytes)]
        self.selected = None
        self.searched = None
        self.logged_out = False

    def select(self, folder, readonly=True):
        self.selected = folder
        return ('OK', [b'3'])

    def login(self, user, password):
        return ('OK', [b'ok'])

    def uid(self, command, *args):
        if command == 'search':
            self.searched = args[-1]
            blob = ' '.join(u for u, _ in self.mails).encode('ascii')
            return ('OK', [blob])
        if command == 'fetch':
            wanted = str(args[0])
            for uid, raw in self.mails:
                if uid == wanted:
                    head = b'%s (RFC822 {%d}' % (uid.encode('ascii'), len(raw))
                    return ('OK', [(head, raw), b')'])
            return ('OK', [None])
        return ('OK', [None])

    def logout(self):
        self.logged_out = True

    def _simple_command(self, *a, **k):
        return ('OK', [b''])

    def _untagged_response(self, *a, **k):
        return ('OK', [b''])


class TestMailEndToEnd(LiveServerCase):

    def seed(self):
        mid = self.new_member('张三', '13800000000', is_owner=True)
        rid = self.new_round('八月采购')
        self.add_item(rid, mid, name='充电机', unit='个', price=6200,
                      purpose='高压电池充电', url='浩泰科技', paid=6200)
        self.add_item(rid, mid, name='绝缘手套', unit='个', qty=2, price=250,
                      purpose='箱检', url='速迫', paid=500)
        return rid

    def test_full_mail_flow(self):
        rid = self.seed()

        # 1) 配邮箱（走 HTTP）
        r = self.client.put('/api/mail/config', json={
            'host': 'imap.example.com', 'user': 'a@example.com', 'password': '授权码',
            'days': 7, 'keywords': ['发票'], 'max_mails': 50,
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn('password', r.json(), '授权码不该回传')

        # 2) 收信：只把网络连接换掉，接口是真的
        conn = FakeImapConn([
            ('1', make_mail('电子发票 浩泰科技 ¥6200.00', 'invoice.pdf', PDF_A)),
            ('2', make_mail('速迫 的发货发票', 'sufa.pdf', PDF_B)),
            ('3', make_mail('闲聊', 'photo.pdf', b'%PDF-1.4 c')),      # 没有发票关键字
        ])
        with mock.patch.object(mailbox, 'open_connection', return_value=conn):
            r = self.client.post('/api/mail/fetch')
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()
        self.assertTrue(got['ok'], got['message'])
        self.assertEqual(got['new'], 2, '第三封不带发票关键字，应当被过滤掉')
        self.assertEqual(got['scanned'], 3)
        self.assertIn('SINCE', conn.searched or '', 'ImapClient 应当把搜索条件下传')
        self.assertEqual(conn.selected, 'INBOX')
        self.assertTrue(conn.logged_out, '服务端自己开的连接，收完应当关掉')

        # 3) 待归档列表带建议
        pending = self.client.get('/api/mail/pending?round_id=%d' % rid).json()
        self.assertEqual(len(pending), 2)
        by_subject = {p['subject']: p for p in pending}
        self.assertEqual(by_subject['电子发票 浩泰科技 ¥6200.00']['suggestions'][0]['name'], '充电机')
        self.assertEqual(by_subject['速迫 的发货发票']['suggestions'][0]['name'], '绝缘手套')

        # 4) 按建议一键挂载
        for row in pending:
            target = row['suggestions'][0]
            r = self.client.post('/api/mail/attach',
                                 json={'mail_id': row['id'], 'item_id': target['item_id']})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()['stored_name'].startswith('DQ-'), r.json()['stored_name'])

        # 5) 归档结果：文件落在 invoices/ 下，名字是 DQ-物品名-金额
        invoices = self.client.get('/api/rounds/%d/invoices' % rid).json()
        self.assertEqual(sorted(v['stored_name'] for v in invoices),
                         ['DQ-充电机-6200.pdf', 'DQ-绝缘手套-500.pdf'])
        for v in invoices:
            path = config.DATA_DIR / v['rel_path']
            self.assertTrue(path.exists(), path)
            self.assertTrue(v['rel_path'].startswith('invoices/八月采购/'))
        self.assertEqual((config.DATA_DIR / invoices[0]['rel_path']).read_bytes() in (PDF_A, PDF_B), True)

        # 6) 两笔都拿到发票了
        d = self.detail(rid)
        self.assertEqual(d['pending_invoice'], [])
        self.assertEqual(len(d['invoices']), 2)
        self.assertTrue(all(i['invoice_status'] == '已收到' for i in d['items']))

        # 7) 导出的采购表里「发票」列应当是「有」
        wb, _ = self.export_wb(rid)
        main = wb['总表']
        self.assertEqual(main.cell(row=3, column=19).value, '有')
        self.assertEqual(main.cell(row=4, column=19).value, '有')

        # 8) 再收一次：同一封邮件不该重复落盘
        conn2 = FakeImapConn([('1', make_mail('电子发票 浩泰科技 ¥6200.00', 'invoice.pdf', PDF_A))])
        with mock.patch.object(mailbox, 'open_connection', return_value=conn2):
            again = self.client.post('/api/mail/fetch').json()
        self.assertEqual(again['new'], 0)
        self.assertTrue(again['skipped'], '已经收过的邮件应当被跳过')

    def test_mail_not_configured_does_not_break_the_page(self):
        r = self.client.post('/api/mail/fetch')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['ok'])
        self.assertTrue(r.json()['lines'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
