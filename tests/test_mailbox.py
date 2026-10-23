# -*- coding: utf-8 -*-
"""邮箱收取核心：配置混淆、邮件头解码、附件解析、收信（客户端可注入）。"""
import base64
import unittest
from datetime import date
from email import message_from_bytes
from email.message import EmailMessage

from app import config
from app.core import mailbox
from tests.support import TempDataTest

PDF = b'%PDF-1.4 fake invoice'


def build_mail(subject='电子发票', filename='invoice.pdf', data=PDF,
               sender='shop@example.com', body='发票见附件'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['Date'] = 'Mon, 06 Oct 2026 10:00:00 +0800'
    msg.set_content(body)
    msg.add_attachment(data, maintype='application', subtype='pdf', filename=filename)
    return msg


def roundtrip(msg):
    """走一遍真实的序列化/解析，而不是直接用构造出来的对象。"""
    return message_from_bytes(msg.as_bytes())


class FakeClient:
    """替代真实 IMAP 连接。"""

    def __init__(self, mails, fail_uids=()):
        self.mails = list(mails)          # [(uid, raw_bytes)]
        self.fail_uids = set(fail_uids)
        self.selected = None
        self.criteria = None
        self.logged_out = False

    def select(self, folder):
        self.selected = folder
        return ('OK', [b'1'])

    def search_uids(self, criteria):
        self.criteria = criteria
        return [uid for uid, _ in self.mails]

    def fetch_raw(self, uid):
        if uid in self.fail_uids:
            raise RuntimeError('boom %s' % uid)
        for u, raw in self.mails:
            if u == uid:
                return raw
        return b''

    def logout(self):
        self.logged_out = True


class TestObfuscate(unittest.TestCase):

    def test_round_trip(self):
        code = 'abcdefg123456'
        blob = mailbox.obfuscate(code)
        self.assertNotEqual(blob, code)
        self.assertNotIn(code, blob)
        self.assertEqual(mailbox.deobfuscate(blob), code)

    def test_empty(self):
        self.assertEqual(mailbox.obfuscate(''), '')
        self.assertEqual(mailbox.deobfuscate(''), '')

    def test_plaintext_in_config_still_works(self):
        """用户手改配置文件填了明文，也要能用（解不出就原样返回）。"""
        self.assertEqual(mailbox.deobfuscate('myPlainCode'), 'myPlainCode')

    def test_chinese_authorization_code(self):
        self.assertEqual(mailbox.deobfuscate(mailbox.obfuscate('授权码测试')), '授权码测试')


class TestMailConfig(unittest.TestCase):

    def test_password_is_obfuscated_in_dict(self):
        cfg = mailbox.MailConfig(host='imap.qq.com', user='a@qq.com', password='secret123')
        d = cfg.to_dict()
        self.assertNotEqual(d['password'], 'secret123')
        self.assertNotIn('secret123', str(d))

    def test_round_trip(self):
        cfg = mailbox.MailConfig(host='imap.qq.com', user='a@qq.com', password='secret123',
                                 folder='收件箱', days=7, unseen_only=True, max_mails=50)
        back = mailbox.MailConfig.from_dict(cfg.to_dict())
        self.assertEqual(back.host, 'imap.qq.com')
        self.assertEqual(back.password, 'secret123')
        self.assertEqual(back.folder, '收件箱')
        self.assertEqual(back.days, 7)
        self.assertTrue(back.unseen_only)
        self.assertEqual(back.max_mails, 50)

    def test_unknown_keys_are_ignored(self):
        back = mailbox.MailConfig.from_dict({'host': 'x', '未来新增的字段': 1})
        self.assertEqual(back.host, 'x')

    def test_ready_needs_host_and_user(self):
        self.assertFalse(mailbox.MailConfig().ready)
        self.assertFalse(mailbox.MailConfig(host='imap.qq.com').ready)
        self.assertTrue(mailbox.MailConfig(host='imap.qq.com', user='a@qq.com').ready)

    def test_defaults_are_sane(self):
        cfg = mailbox.MailConfig()
        self.assertEqual(cfg.port, 993)
        self.assertTrue(cfg.use_ssl)
        self.assertEqual(cfg.folder, 'INBOX')
        self.assertIn('发票', cfg.keywords)
        self.assertEqual(cfg.auto_interval, 0, '默认不开启定时收取')


class TestConfigFile(TempDataTest):

    def test_missing_file_gives_defaults(self):
        cfg = mailbox.load_config()
        self.assertEqual(cfg.host, '')
        self.assertFalse(cfg.ready)

    def test_save_then_load(self):
        cfg = mailbox.MailConfig(host='imap.163.com', user='a@163.com', password='code',
                                 days=15, auto_interval=30)
        path = mailbox.save_config(cfg)
        self.assertTrue(path.exists())

        raw = path.read_text(encoding='utf-8')
        self.assertNotIn('"code"', raw, '授权码不能明文写进配置文件')

        back = mailbox.load_config()
        self.assertEqual(back.host, 'imap.163.com')
        self.assertEqual(back.password, 'code')
        self.assertEqual(back.days, 15)
        self.assertEqual(back.auto_interval, 30)

    def test_broken_file_does_not_crash(self):
        config.MAIL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.MAIL_CONFIG.write_text('{ 不是 json', encoding='utf-8')
        self.assertEqual(mailbox.load_config().host, '')


class TestImapDate(unittest.TestCase):
    """IMAP 只认英文月份；不能依赖 strftime（中文 Windows 会给「1月」）。"""

    def test_english_months(self):
        self.assertEqual(mailbox.imap_date(date(2026, 1, 5)), '05-Jan-2026')
        self.assertEqual(mailbox.imap_date(date(2026, 12, 31)), '31-Dec-2026')
        self.assertEqual(mailbox.imap_date(date(2026, 9, 1)), '01-Sep-2026')

    def test_all_months_are_english(self):
        for m in range(1, 13):
            got = mailbox.imap_date(date(2026, m, 15))
            self.assertRegex(got, r'^\d{2}-[A-Z][a-z]{2}-\d{4}$', got)


class TestCriteria(unittest.TestCase):

    def test_days_only(self):
        cfg = mailbox.MailConfig(days=30)
        self.assertEqual(mailbox.build_criteria(cfg, date(2026, 10, 7)), 'SINCE 07-Sep-2026')

    def test_unseen_only(self):
        cfg = mailbox.MailConfig(days=0, unseen_only=True)
        self.assertEqual(mailbox.build_criteria(cfg), 'UNSEEN')

    def test_both(self):
        cfg = mailbox.MailConfig(days=7, unseen_only=True)
        self.assertEqual(mailbox.build_criteria(cfg, date(2026, 10, 7)), 'SINCE 30-Sep-2026 UNSEEN')

    def test_neither_is_all(self):
        self.assertEqual(mailbox.build_criteria(mailbox.MailConfig(days=0)), 'ALL')

    def test_scan_read_drops_the_unseen_filter(self):
        """勾了「也扫已读」，就不能再带 UNSEEN —— 否则已读邮件永远不看。"""
        cfg = mailbox.MailConfig(days=0, unseen_only=True, scan_read=True)
        self.assertEqual(mailbox.build_criteria(cfg), 'ALL')


class TestHeaderDecode(unittest.TestCase):

    def test_plain(self):
        self.assertEqual(mailbox.decode_mime_header('Invoice'), 'Invoice')

    def test_base64_utf8(self):
        raw = '=?utf-8?B?55S15a2Q5Y+R56Wo?='
        self.assertEqual(mailbox.decode_mime_header(raw), '电子发票')

    def test_quoted_printable(self):
        raw = '=?utf-8?Q?=E5=8F=91=E7=A5=A8?='
        self.assertEqual(mailbox.decode_mime_header(raw), '发票')

    def test_gb2312_fallback(self):
        text = '发票'.encode('gb18030')
        raw = '=?gb2312?B?%s?=' % base64.b64encode(text).decode('ascii')
        self.assertEqual(mailbox.decode_mime_header(raw), '发票')

    def test_empty(self):
        self.assertEqual(mailbox.decode_mime_header(''), '')
        self.assertEqual(mailbox.decode_mime_header(None), '')

    def test_garbage_does_not_crash(self):
        mailbox.decode_mime_header('=?utf-8?B?这不是base64!?=')


class TestSafeFilename(unittest.TestCase):

    def test_strips_paths(self):
        self.assertEqual(mailbox.safe_filename('a/b/c.pdf'), 'c.pdf')
        self.assertEqual(mailbox.safe_filename('..\\..\\x.pdf'), 'x.pdf')

    def test_replaces_illegal_chars(self):
        got = mailbox.safe_filename('a:b*c?d.pdf')
        for ch in ':*?"<>|':
            self.assertNotIn(ch, got, '非法字符必须被替换掉：%r' % got)
        self.assertTrue(got.endswith('.pdf'), got)

    def test_blank_uses_fallback(self):
        self.assertEqual(mailbox.safe_filename('', 'attachment.pdf'), 'attachment.pdf')
        self.assertEqual(mailbox.safe_filename('   '), 'attachment')


class TestAttachmentParsing(unittest.TestCase):

    def test_picks_pdf_attachment(self):
        msg = roundtrip(build_mail())
        got = mailbox.iter_attachments(msg)
        self.assertEqual(len(got), 1)
        name, data = got[0]
        self.assertEqual(name, 'invoice.pdf')
        self.assertEqual(data, PDF)

    def test_filter_keeps_invoice_like(self):
        msg = roundtrip(build_mail(subject='电子发票', filename='发票.pdf'))
        self.assertEqual(len(mailbox.filter_attachments(msg, mailbox.MailConfig())), 1)

    def test_filter_drops_non_invoice_extension(self):
        msg = roundtrip(build_mail(filename='病毒.exe', data=b'MZ'))
        self.assertEqual(mailbox.filter_attachments(msg, mailbox.MailConfig()), [])

    def test_filter_drops_when_keyword_missing(self):
        msg = roundtrip(build_mail(subject='闲聊', filename='photo.pdf'))
        self.assertEqual(mailbox.filter_attachments(msg, mailbox.MailConfig()), [])

    def test_any_attachment_overrides_keywords(self):
        msg = roundtrip(build_mail(subject='闲聊', filename='photo.pdf'))
        cfg = mailbox.MailConfig(any_attachment=True)
        self.assertEqual(len(mailbox.filter_attachments(msg, cfg)), 1)

    def test_custom_keywords(self):
        msg = roundtrip(build_mail(subject='报销凭据', filename='a.pdf'))
        self.assertEqual(mailbox.filter_attachments(msg, mailbox.MailConfig()), [])
        cfg = mailbox.MailConfig(keywords=['报销'])
        self.assertEqual(len(mailbox.filter_attachments(msg, cfg)), 1)

    def test_multiple_attachments(self):
        msg = EmailMessage()
        msg['Subject'] = '发票'
        msg['From'] = 'shop@example.com'
        msg.set_content('x')
        msg.add_attachment(PDF, maintype='application', subtype='pdf', filename='一.pdf')
        msg.add_attachment(PDF + b'2', maintype='application', subtype='pdf', filename='二.pdf')
        got = mailbox.iter_attachments(roundtrip(msg))
        self.assertEqual(sorted(n for n, _ in got), ['一.pdf', '二.pdf'])

    def test_image_attachment(self):
        msg = build_mail(filename='发票.png', data=b'\x89PNG')
        picked = mailbox.filter_attachments(roundtrip(msg), mailbox.MailConfig())
        self.assertEqual(picked[0][0], '发票.png')


class TestFetchAttachments(unittest.TestCase):

    def cfg(self, **over):
        base = {'host': 'imap.example.com', 'user': 'a@example.com', 'password': 'x',
                'days': 0, 'max_mails': 50}
        base.update(over)
        return mailbox.MailConfig(**base)

    def test_collects_attachments(self):
        mails = [('1', build_mail(subject='发票一').as_bytes()),
                 ('2', build_mail(subject='发票二', filename='二.pdf').as_bytes())]
        client = FakeClient(mails)
        r = mailbox.fetch_attachments(self.cfg(), client=client)
        self.assertTrue(r['ok'], r['message'])
        self.assertEqual(r['scanned'], 2)
        self.assertEqual(len(r['attachments']), 2)
        self.assertEqual(r['attachments'][0]['filename'], 'invoice.pdf')
        self.assertEqual(r['attachments'][0]['data'], PDF)
        self.assertEqual(r['attachments'][0]['sha1'], mailbox.sha1_of(PDF))
        self.assertIn('发票一', r['attachments'][0]['subject'])
        self.assertEqual(client.selected, 'INBOX')
        self.assertFalse(client.logged_out, '注入的客户端不该被关掉，谁开谁关')

    def test_skips_already_fetched_uids(self):
        mails = [('1', build_mail().as_bytes()), ('2', build_mail(filename='b.pdf').as_bytes())]
        r = mailbox.fetch_attachments(self.cfg(), client=FakeClient(mails), skip_uids=['1'])
        self.assertEqual([a['uid'] for a in r['attachments']], ['2'])
        self.assertEqual(r['skipped'][0]['uid'], '1')
        self.assertIn('收过', r['skipped'][0]['reason'])

    def test_mail_without_invoice_attachment_is_skipped(self):
        mails = [('1', build_mail(subject='闲聊', filename='x.exe', data=b'MZ').as_bytes())]
        r = mailbox.fetch_attachments(self.cfg(), client=FakeClient(mails))
        self.assertEqual(r['attachments'], [])
        self.assertEqual(r['skipped'][0]['uid'], '1')
        self.assertIn('没有像发票的附件', r['skipped'][0]['reason'])

    def test_one_bad_mail_does_not_stop_the_rest(self):
        mails = [('1', build_mail(subject='好的').as_bytes()),
                 ('2', build_mail(subject='坏的').as_bytes())]
        client = FakeClient(mails, fail_uids=['2'])
        r = mailbox.fetch_attachments(self.cfg(), client=client)
        self.assertTrue(r['ok'])
        self.assertEqual(len(r['attachments']), 1)
        self.assertTrue(any('2' in e for e in r['errors']))

    def test_search_criteria_is_passed_down(self):
        client = FakeClient([])
        mailbox.fetch_attachments(self.cfg(days=7, unseen_only=True), client=client)
        self.assertTrue(client.criteria.startswith('SINCE '))
        self.assertIn('UNSEEN', client.criteria)

    def test_max_mails_keeps_the_newest(self):
        mails = [('%d' % i, build_mail().as_bytes()) for i in range(1, 11)]
        r = mailbox.fetch_attachments(self.cfg(max_mails=3), client=FakeClient(mails))
        self.assertEqual(r['scanned'], 3)
        self.assertEqual([a['uid'] for a in r['attachments']], ['8', '9', '10'])

    def test_log_callback_is_used(self):
        lines = []
        mailbox.fetch_attachments(self.cfg(), client=FakeClient([('1', build_mail().as_bytes())]),
                                  on_log=lines.append)
        self.assertTrue(any('搜索条件' in ln for ln in lines))

    def test_missing_host_or_user_is_reported_not_raised(self):
        for cfg in (mailbox.MailConfig(), mailbox.MailConfig(host='x')):
            r = mailbox.fetch_attachments(cfg, client=FakeClient([]))
            self.assertFalse(r['ok'])
            self.assertTrue(r['message'])

    def test_client_error_is_caught(self):
        class Boom(FakeClient):
            def search_uids(self, criteria):
                raise RuntimeError('IMAP 断了')

        r = mailbox.fetch_attachments(self.cfg(), client=Boom([]))
        self.assertFalse(r['ok'])
        self.assertIn('IMAP 断了', r['message'])


class TestErrorText(unittest.TestCase):

    def test_bytes_exception_args_are_decoded(self):
        class E(Exception):
            pass
        self.assertEqual(mailbox.error_text(E(b'LOGIN failed')), 'LOGIN failed')

    def test_gb18030_bytes(self):
        class E(Exception):
            pass
        self.assertEqual(mailbox.error_text(E('登录失败'.encode('gb18030'))), '登录失败')

    def test_plain_string(self):
        self.assertEqual(mailbox.error_text(ValueError('普通错误')), '普通错误')


class TestConnection(unittest.TestCase):

    def test_not_configured_returns_not_ok_without_network(self):
        r = mailbox.test_connection(mailbox.MailConfig())
        self.assertFalse(r['ok'])
        self.assertIn('请先填写', r['message'])

    def test_open_connection_requires_host(self):
        with self.assertRaises(ValueError):
            mailbox.open_connection(mailbox.MailConfig())


class TestPresets(unittest.TestCase):

    def test_has_the_common_providers(self):
        names = [p[0] for p in mailbox.PRESETS]
        for name in ('QQ邮箱', '163邮箱', '腾讯企业邮箱', 'Gmail', '自定义'):
            self.assertIn(name, names)

    def test_qq_and_163_hosts(self):
        by_name = {p[0]: p[1] for p in mailbox.PRESETS}
        self.assertEqual(by_name['QQ邮箱'], 'imap.qq.com')
        self.assertEqual(by_name['163邮箱'], 'imap.163.com')
        self.assertEqual(by_name['腾讯企业邮箱'], 'imap.exmail.qq.com')

    def test_all_use_993(self):
        for _name, _host, port in mailbox.PRESETS:
            self.assertEqual(port, 993)


class TestPython38Compat(unittest.TestCase):
    """这几个坑**只有 Python 3.8 才暴露**（开发机是 3.9）。

    阿里云 ECS 自带的就是 3.8 —— 所以这不是理论问题，真机上就是这么炸的：
    邮箱一配好、一点「测试连接」就报错。本地怎么测都是绿的。
    """

    def _src(self):
        import inspect
        return inspect.getsource(mailbox.open_connection)

    def test_imap_is_not_given_a_timeout_kwarg(self):
        """`timeout` 是 Python 3.9 才加的，3.8 传进去直接 TypeError。"""
        head = self._src().split('conn.login')[0]
        self.assertNotIn('timeout=', head,
                         '别给 IMAP4 / IMAP4_SSL 传 timeout —— 3.8 上会炸')

    def test_the_id_command_is_registered(self):
        """imaplib 默认没有 ID；不注册的话自报身份会静默失败。"""
        import imaplib
        self.assertIn('ID', imaplib.Commands)

    def test_the_id_is_sent_after_login(self):
        """163 把「登录前就报身份」的客户端判成不安全，必须放在 login 之后。"""
        src = self._src()
        self.assertLess(src.index('conn.login'),
                        src.index('send_client_id(conn)'),
                        'send_client_id 要在 login 之后调用')


class FakeResp:
    """替代 urllib 的响应对象（下载发票链接时用）。"""

    def __init__(self, data, ctype='application/octet-stream'):
        self._data = data
        self.headers = {'Content-Type': ctype}

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


ALI_LINK = ('https://einvoice.taobao.com//api/invoice/downloadMailInvoice'
            '?token=017l-3RwGLesBWCJ&from=mail')


def build_html_mail(html, subject='【阿里发票平台】电子发票附件查收',
                    sender='noreply@notice.invoice-mail.taobao.com'):
    """淘宝/阿里那种**没有附件、正文里只有下载链接**的邮件。"""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['Date'] = 'Mon, 06 Oct 2026 10:00:00 +0800'
    msg.set_content(html, subtype='html')
    return msg


class TestInvoiceLinkHosts(unittest.TestCase):
    """白名单是**安全边界**：邮件谁都能发来，不能让它指挥服务器去请求任意地址。"""

    def test_the_platform_domain_is_allowed(self):
        self.assertTrue(mailbox.host_allowed(ALI_LINK))

    def test_a_subdomain_is_allowed(self):
        self.assertTrue(mailbox.host_allowed('https://einvoice.jd.com/x'))

    def test_a_lookalike_domain_is_refused(self):
        """`evil-taobao.com` 结尾也带 taobao.com —— 必须靠点号卡死。"""
        self.assertFalse(mailbox.host_allowed('https://evil-taobao.com/x'))
        self.assertFalse(mailbox.host_allowed('https://taobao.com.evil.net/x'))

    def test_plain_http_is_refused(self):
        self.assertFalse(mailbox.host_allowed('http://einvoice.taobao.com/x'))

    def test_an_unrelated_host_is_refused(self):
        self.assertFalse(mailbox.host_allowed('https://example.com/invoice.pdf'))

    def test_internal_addresses_are_refused(self):
        """别把自己当成 SSRF 跳板去探内网。"""
        for u in ('https://127.0.0.1/invoice', 'https://localhost/x',
                  'https://169.254.169.254/latest/meta-data/'):
            self.assertFalse(mailbox.host_allowed(u), u)


class TestExtractInvoiceLinks(unittest.TestCase):

    def test_pulls_the_download_link_out_of_the_html(self):
        html = '<a href="%s">下载发票</a>' % ALI_LINK
        links = mailbox.extract_invoice_links(roundtrip(build_html_mail(html)))
        self.assertEqual(links, [ALI_LINK])

    def test_html_escaped_ampersand_is_decoded(self):
        html = '<a href="%s">下载</a>' % ALI_LINK.replace('&', '&amp;')
        self.assertEqual(mailbox.extract_invoice_links(roundtrip(build_html_mail(html))),
                         [ALI_LINK])

    def test_a_non_platform_link_is_ignored(self):
        html = '<a href="https://evil.com/invoice/download?f=1">下载发票</a>'
        self.assertEqual(mailbox.extract_invoice_links(roundtrip(build_html_mail(html))), [])

    def test_a_platform_link_that_is_not_an_invoice_is_ignored(self):
        """淘宝邮件里还夹着「联系卖家」这类链接，别一起拉下来。"""
        html = '<a href="https://einvoice.taobao.com//help/index.html">帮助</a>'
        self.assertEqual(mailbox.extract_invoice_links(roundtrip(build_html_mail(html))), [])

    def test_duplicate_links_appear_once(self):
        html = '<a href="%s">a</a><img src="%s">' % (ALI_LINK, ALI_LINK)
        self.assertEqual(len(mailbox.extract_invoice_links(roundtrip(build_html_mail(html)))), 1)

    def test_a_mail_without_html_gives_nothing(self):
        self.assertEqual(mailbox.extract_invoice_links(roundtrip(build_mail())), [])


class TestDownloadInvoiceLink(unittest.TestCase):

    def test_downloads_a_pdf_and_names_it(self):
        name, data = mailbox.download_invoice_link(
            ALI_LINK, opener=lambda r: FakeResp(b'%PDF-1.5 real invoice'))
        self.assertTrue(name.endswith('.pdf'), name)
        self.assertEqual(data, b'%PDF-1.5 real invoice')

    def test_a_png_is_recognised_by_content_not_by_header(self):
        """平台给的 Content-Type 常是 octet-stream，只能靠魔数认。"""
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 40
        name, _ = mailbox.download_invoice_link(
            ALI_LINK, opener=lambda r: FakeResp(png))
        self.assertTrue(name.endswith('.png'), name)

    def test_a_non_platform_url_is_refused_even_here(self):
        with self.assertRaises(ValueError):
            mailbox.download_invoice_link('https://evil.com/invoice.pdf',
                                          opener=lambda r: FakeResp(b'%PDF-1.5'))

    def test_an_oversized_file_is_refused(self):
        big = b'%PDF-1.5' + b'0' * (mailbox.LINK_MAX_BYTES + 10)
        with self.assertRaises(ValueError):
            mailbox.download_invoice_link(ALI_LINK, opener=lambda r: FakeResp(big))

    def test_an_empty_response_is_refused(self):
        with self.assertRaises(ValueError):
            mailbox.download_invoice_link(ALI_LINK, opener=lambda r: FakeResp(b''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
