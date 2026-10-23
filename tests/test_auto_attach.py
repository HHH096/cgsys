# -*- coding: utf-8 -*-
"""收到发票后**自动挂到条目上** —— 但只在「不会挂错」的时候。

规则很简单也很硬：**挂错比不挂糟得多**。
票挂到错条目上，对账、合并打印、报销全跟着错，而且很难发现。
所以只有同时满足两条才敢自动挂：

1. 只有一个候选（出现第二个就说明有歧义）；
2. 分数 ≥ 10，也就是**票面金额和那条的实付/合计精确对得上**。
   光靠名称/商家命中（6/4 分）不作数。
"""
import unittest

from app import config
from app.core import mailfetch, mailbox, repo
from tests.support import TempDataTest
from tests.test_mailbox import FakeClient, build_mail


class AutoAttachCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.rid = repo.create_round('九月采购')
        self.iid = self.add_item('电容', paid=22.0)

    def add_item(self, name, paid=22.0, price=None):
        data = {'name': name, 'qty': 1, 'unit': '个', 'price': price or paid,
                'purpose': '用', 'applicant': '张三', 'paid': paid}
        iid = repo.add_item(self.rid, self.mid, data)
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def collect(self, subject='电子发票 ¥22.00', **over):
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0, **over)
        return mailfetch.collect(
            cfg, client=FakeClient([('1', build_mail(subject=subject).as_bytes())]))

    def rows(self):
        return repo.list_mail_attachments()


class TestWhenItAttaches(AutoAttachCase):

    def test_a_unique_amount_match_is_attached(self):
        r = self.collect()
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(r['auto_attached'][0]['item_id'], self.iid)
        self.assertEqual(r['auto_attached'][0]['item_name'], '电容')

    def test_it_leaves_the_pending_list(self):
        r = self.collect()
        self.assertEqual(r['saved'], [], '自动挂上的要说在 auto_attached 里，不在 saved')
        self.assertEqual(self.rows()[0]['status'], repo.MAIL_ATTACHED)

    def test_it_is_marked_as_auto(self):
        """人得能一眼看出「这张我没点过」—— 否则不敢信。"""
        self.collect()
        self.assertEqual(self.rows()[0]['auto_attached'], 1)

    def test_the_item_now_counts_as_having_an_invoice(self):
        self.collect()
        self.assertEqual(repo.get_item(self.iid)['invoice_status'],
                         config.INV_RECEIVED)

    def test_the_file_is_archived_as_a_real_invoice(self):
        self.collect()
        invs = repo.list_invoices(round_id=self.rid)
        self.assertEqual(len(invs), 1)
        self.assertTrue((config.DATA_DIR / invs[0]['rel_path']).exists())

    def test_a_manual_attach_is_marked_as_not_auto(self):
        """人点的不该被标成自动 —— 不然这个标记就没意义了。"""
        r = self.collect(auto_attach=False)
        mid = r['saved'][0]['id']
        mailfetch.attach_to_item(mid, self.iid)
        self.assertEqual(self.rows()[0]['auto_attached'], 0)


class TestWhenItRefuses(AutoAttachCase):
    """这些情况下必须**忍住不挂**，把票留在待归档让人看。"""

    def test_two_items_with_the_same_amount_are_not_attached(self):
        """两条金额一样（很常见）→ 有歧义，不挂。"""
        self.add_item('另一个电容', paid=22.0)
        r = self.collect()
        self.assertEqual(r['auto_attached'], [])
        self.assertEqual(len(r['saved']), 1, '应当留在待归档')
        self.assertEqual(self.rows()[0]['status'], repo.MAIL_PENDING)

    def test_a_name_hit_alone_is_not_enough(self):
        """主题里只有名称、没有金额 → 分数不够，不挂。"""
        r = self.collect(subject='电子发票 电容')
        self.assertEqual(r['auto_attached'], [])

    def test_no_clue_at_all_is_not_attached(self):
        r = self.collect(subject='电子发票 ¥999.00')
        self.assertEqual(r['auto_attached'], [])

    def test_the_switch_off_keeps_everything_pending(self):
        r = self.collect(auto_attach=False)
        self.assertEqual(r['auto_attached'], [])
        self.assertEqual(len(r['saved']), 1)

    def test_an_item_that_already_has_an_invoice_is_out_of_the_pool(self):
        """已经收到票的条目不再当候选 —— 免得给同一笔挂第二张。"""
        first = self.collect()
        self.assertEqual(len(first['auto_attached']), 1)
        # 再来一张同样金额的票（内容要不一样，否则会被 sha1 去重当成重复附件）
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0)
        again = mailfetch.collect(
            cfg, client=FakeClient([('2', build_mail(
                subject='电子发票 ¥22.00', filename='b.pdf',
                data=b'%PDF-1.4 second invoice').as_bytes())]))
        self.assertEqual(again['auto_attached'], [])
        self.assertEqual(len(again['saved']), 1, '应当留在待归档等人核对')


class TestTheReportSaysSo(AutoAttachCase):
    """收信之后要明确告诉人「我自动挂了几张」。"""

    def test_the_report_line_mentions_the_auto_attach(self):
        r = self.collect()
        text = '\n'.join(mailfetch.report_lines(r))
        self.assertIn('自动挂', text)
        self.assertIn('电容', text)

    def test_nothing_is_said_when_nothing_was_attached(self):
        r = self.collect(subject='电子发票 ¥999.00')
        text = '\n'.join(mailfetch.report_lines(r))
        self.assertNotIn('自动挂', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)


def money_pdf(amount, tag=''):
    """造一张**有文字层**的发票 PDF（金额只在票面里，标题和文件名都不带）。

    `tag` 用来让两张金额相同的 PDF 字节不同 —— 否则会被 sha1 去重当成重复附件。
    """
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((12, 40), 'Total %.2f' % amount, fontsize=12)
    if tag:
        page.insert_text((12, 80), tag, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


class TestTheAmountComesFromTheInvoice(AutoAttachCase):
    """**金额在票面里，不在邮件主题/文件名里** —— 真实邮件就是这样。

    踩过的坑：只从主题/文件名抠数字，而这些邮件叫「李锐涛发票10月」、
    附件名是发票号 `dzfp_2641...pdf`，两处都没有金额 ——
    于是候选恒为 0，自动挂永远不会触发（线上 27 张票全是 0 候选）。
    """

    def real_looking_mail(self, amount, subject='李锐涛发票10月',
                          filename='dzfp_26412000004041153361_某店_20261023.pdf'):
        return mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject=subject, filename=filename,
                data=money_pdf(amount)).as_bytes())]))

    def test_it_matches_on_the_amount_inside_the_pdf(self):
        r = self.real_looking_mail(22.0)
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(r['auto_attached'][0]['item_name'], '电容')

    def test_twenty_digit_invoice_numbers_are_not_amounts(self):
        """文件名里那串 20 位发票号不能被当成金额（否则会乱配）。"""
        r = self.real_looking_mail(22.0,
                                   filename='dzfp_26412000004041153361.pdf')
        self.assertEqual(len(r['auto_attached']), 1, r)

    def test_a_mismatched_amount_still_refuses(self):
        """票面金额对不上就不挂 —— 读金额是为了更准，不是放松门槛。"""
        r = self.real_looking_mail(999.0)
        self.assertEqual(r['auto_attached'], [])
        self.assertEqual(len(r['saved']), 1)

    def test_a_scanned_pdf_without_text_stays_manual(self):
        """没有文字层的照片票读不出金额 → 留在待归档，本来就该人工确认。"""
        r = mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject='李锐涛发票10月', filename='photo.pdf',
                data=b'%PDF-1.4 no text layer').as_bytes())]))
        self.assertEqual(r['auto_attached'], [])
        self.assertEqual(len(r['saved']), 1)


class TestOneInvoiceCoversSeveralItems(AutoAttachCase):
    """**一票多条**：淘宝一个订单买几样，商家只开一张票，票面是几条之和。

    单条永远对不上，这种票以前只能人工挂。现在能认出「哪几条加起来正好」，
    但**只在只有一种拼法**时才敢动手。
    """

    def setUp(self):
        super().setUp()
        # AutoAttachCase 已经建了一个 paid=22 的「电容」；再加两条能拼成 192
        self.a = self.add_item('交流航空插头', paid=160.0)
        self.b = self.add_item('胶泥', paid=32.0)

    def collect_amount(self, amount, filename='multi.pdf'):
        return mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject='李锐涛发票10月', filename=filename,
                data=money_pdf(amount)).as_bytes())]))

    def test_it_attaches_to_every_item_in_the_only_combination(self):
        r = self.collect_amount(192.0)
        self.assertEqual(len(r['auto_attached']), 1, r)
        got = r['auto_attached'][0]
        self.assertEqual(sorted(got['item_ids']), sorted([self.a, self.b]))

    def test_both_items_now_count_as_having_an_invoice(self):
        self.collect_amount(192.0)
        for iid in (self.a, self.b):
            self.assertEqual(repo.get_item(iid)['invoice_status'],
                             config.INV_RECEIVED, '条目 %s' % iid)

    def test_it_produces_only_one_invoice_record(self):
        """两张条目共享**同一张**票 —— 不能复制成两条发票记录（会重复报销）。"""
        self.collect_amount(192.0)
        invs = repo.list_invoices(round_id=self.rid)
        self.assertEqual(len(invs), 1)
        self.assertEqual(len(repo.items_of_invoice(invs[0]['id'])), 2)

    def test_the_mail_record_is_marked_auto(self):
        self.collect_amount(192.0)
        rows = [x for x in repo.list_mail_attachments() if x['status'] == 'attached']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['auto_attached'], 1)


class TestCombinationRefusals(AutoAttachCase):

    def setUp(self):
        super().setUp()
        self.a = self.add_item('热缩管', paid=34.0)

    def test_two_ways_to_split_means_refuse(self):
        """¥80 既能是「面板航插 46 + 热缩管 34」，也能是别的拼法 → 不挂。"""
        b = self.add_item('面板航插', paid=46.0)      # 34 + 46 = 80
        c = self.add_item('别的', paid=34.0)          # 34 + 46 也能由 c + b 拼出来
        r = mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject='李瑞涛发票10月', filename='amb.pdf',
                data=money_pdf(80.0)).as_bytes())]))
        self.assertEqual(r['auto_attached'], [], '有两种拼法就不该自动挂')
        self.assertEqual(len(r['saved']), 1)

    def test_a_single_item_match_wins_over_combinations(self):
        """单条能精确对上时就走单条，不去凑组合 —— 单条是更强的证据。"""
        iid = self.add_item('整笔', paid=100.0)
        self.add_item('零头一', paid=40.0)
        self.add_item('零头二', paid=60.0)          # 40+60 也能拼成 100
        r = mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject='李涛发票10月', filename='one.pdf',
                data=money_pdf(100.0)).as_bytes())]))
        self.assertEqual(len(r['auto_attached']), 1)
        self.assertEqual(r['auto_attached'][0]['item_ids'], [iid])


class TestSumCombinationsUnit(unittest.TestCase):
    """组合搜索本身的边界。"""

    def it(self, iid, amount, rid=1):
        return {'id': iid, 'name': 'x', 'paid': amount, 'total': amount,
                'round_id': rid}

    def test_two_items_that_add_up(self):
        got = mailfetch.sum_combinations(192.0, [self.it(1, 160.0), self.it(2, 32.0)])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(x['id'] for x in got[0]), [1, 2])

    def test_no_combination(self):
        self.assertEqual(mailfetch.sum_combinations(999.0, [self.it(1, 10.0)]), [])

    def test_it_does_not_mix_rounds(self):
        """跨轮次的条目不能拼成一张票 —— 说不清算哪个月的账。"""
        got = mailfetch.sum_combinations(50.0, [self.it(1, 20.0, rid=1),
                                                self.it(2, 30.0, rid=2)])
        self.assertEqual(got, [])

    def test_tolerance_is_two_cents(self):
        self.assertEqual(len(mailfetch.sum_combinations(
            50.02, [self.it(1, 20.0), self.it(2, 30.0)])), 1)
        self.assertEqual(mailfetch.sum_combinations(
            50.5, [self.it(1, 20.0), self.it(2, 30.0)]), [])

    def test_three_items(self):
        got = mailfetch.sum_combinations(100.0, [self.it(1, 20.0), self.it(2, 30.0),
                                                 self.it(3, 50.0)])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(x['id'] for x in got[0]), [1, 2, 3])


class TestSmallerCombinationsWin(AutoAttachCase):
    """**2 条唯一就定，别看 3 条。**

    踩过的坑：一开始把 2 条和 3 条一起找，结果条目里一堆小额让 3 条凑出几十种
    （¥192 用 2 条只 1 种、用 3 条有 42 种），把本来唯一的答案冲掉了 ——
    线上有 2 张票本该挂上却被拒。
    """

    def collect_amount(self, amount):
        return mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([('1', build_mail(
                subject='李锐涛发票10月', filename='pair.pdf',
                data=money_pdf(amount)).as_bytes())]))

    def test_a_unique_pair_is_accepted_even_when_triples_also_add_up(self):
        a = self.add_item('大件', paid=160.0)
        b = self.add_item('小件', paid=32.0)
        # 让 3 条也能凑出 192：100 + 50 + 42
        self.add_item('百', paid=100.0)
        self.add_item('五十', paid=50.0)
        self.add_item('四二', paid=42.0)

        r = self.collect_amount(192.0)
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(sorted(r['auto_attached'][0]['item_ids']), sorted([a, b]))

    def test_an_ambiguous_pair_refuses_and_does_not_fall_through_to_triples(self):
        """2 条就有两种拼法 → 停，不要再去试 3 条碰运气。"""
        self.add_item('甲', paid=100.0)
        self.add_item('乙', paid=92.0)
        self.add_item('丙', paid=100.0)      # 丙+乙 也能拼成 192
        r = self.collect_amount(192.0)
        self.assertEqual(r['auto_attached'], [])

    def test_the_search_is_per_size(self):
        items = [{'id': 1, 'name': 'a', 'paid': 160.0, 'total': 160.0, 'round_id': 1},
                 {'id': 2, 'name': 'b', 'paid': 32.0, 'total': 32.0, 'round_id': 1},
                 {'id': 3, 'name': 'c', 'paid': 100.0, 'total': 100.0, 'round_id': 1},
                 {'id': 4, 'name': 'd', 'paid': 50.0, 'total': 50.0, 'round_id': 1},
                 {'id': 5, 'name': 'e', 'paid': 42.0, 'total': 42.0, 'round_id': 1}]
        pairs = mailfetch.sum_combinations(192.0, items, min_items=2, max_items=2)
        triples = mailfetch.sum_combinations(192.0, items, min_items=3, max_items=3)
        self.assertEqual(len(pairs), 1, '2 条应当唯一')
        self.assertEqual(len(triples), 1, '3 条也恰好一种，但调用方应停在 2 条')


class TestOneInvoiceCoversSeveralItems(TempDataTest):
    """一张票盖了好几笔采购 —— 淘宝一个订单买几样、商家只开一张票。

    票面金额是几条之和，单条永远对不上。以前这种只能人工挂，
    现在只要「拼法唯一」就自动挂到那几条上。
    """

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.rid = repo.create_round('十月采购')

    def add(self, name, paid):
        return repo.add_item(self.rid, self.mid, {
            'name': name, 'qty': 1, 'unit': '个', 'price': paid,
            'purpose': 'x', 'applicant': '张三', 'paid': paid})

    def collect(self, amount, filename='inv.pdf'):
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0)
        return mailfetch.collect(cfg, client=FakeClient(
            [('1', build_mail(subject='发票', filename=filename,
                              data=money_pdf(amount)).as_bytes())]))

    def test_a_unique_two_item_sum_is_attached_to_both(self):
        a = self.add('热缩管', 34.0)
        b = self.add('面板航插', 46.0)
        r = self.collect(80.0)
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(repo.get_item(a)['invoice_status'], config.INV_RECEIVED)
        self.assertEqual(repo.get_item(b)['invoice_status'], config.INV_RECEIVED)
        self.assertEqual(len(repo.list_invoices(round_id=self.rid)), 1,
                         '一张票只登记一次，不要给每条都复制一份')

    def test_both_items_are_linked_to_the_same_invoice(self):
        a = self.add('热缩管', 34.0)
        b = self.add('面板航插', 46.0)
        self.collect(80.0)
        inv = repo.list_invoices(round_id=self.rid)[0]
        linked = sorted(x['id'] for x in repo.items_of_invoice(inv['id']))
        self.assertEqual(linked, sorted([a, b]))

    def test_an_ambiguous_split_is_not_attached(self):
        """有多种拼法就不挂 —— 拼错等于把账记到别人头上。

        这里特意让**单条都对不上**（没有 30 的条目），否则会走「单条优先」那条路。
        10+20=30、15+15=30 —— 两种拼法，必须忍住。
        """
        for nm, v in (('甲', 10.0), ('乙', 20.0), ('丙', 15.0), ('丁', 15.0)):
            self.add(nm, v)
        r = self.collect(30.0)
        self.assertEqual(r['auto_attached'], [])
        self.assertEqual(len(r['saved']), 1)

    def test_a_single_item_exact_match_wins_over_combinations(self):
        """单条就能对上时不去凑组合 —— 单条是更强的证据。"""
        one = self.add('整件', 80.0)
        self.add('甲', 34.0)
        self.add('乙', 46.0)
        r = self.collect(80.0)
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(repo.get_item(one)['invoice_status'], config.INV_RECEIVED)

    def test_items_from_different_rounds_are_never_combined(self):
        """跨轮次不能拼 —— 一张票说不清算哪个月的账。"""
        other = repo.create_round('九月采购')
        a = self.add('甲', 34.0)
        b = repo.add_item(other, self.mid, {'name': '乙', 'qty': 1, 'unit': '个',
                                            'price': 46, 'purpose': 'x',
                                            'applicant': '张三', 'paid': 46})
        r = self.collect(80.0)
        self.assertEqual(r['auto_attached'], [])


class TestTwoInvoicesInOneFetch(AutoAttachCase):
    """同一次收信里的两张票 —— 一条采购绝不能被挂上两张。

    这是个真实踩过的坑：候选池在循环**外面**只算一次，
    第一张挂走条目之后，池子里还有它，第二张就会再挂一次 ——
    等于一条采购两张发票，合并打印重复、报销重复，而且事后极难发现。
    """

    def test_only_the_first_one_is_attached(self):
        r = mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([
                ('1', build_mail(subject='invoice', filename='a.pdf',
                                 data=money_pdf(22.0, 'A')).as_bytes()),
                ('2', build_mail(subject='invoice', filename='b.pdf',
                                 data=money_pdf(22.0, 'B')).as_bytes()),
            ]))
        self.assertEqual(len(r['auto_attached']), 1, r)
        self.assertEqual(len(r['saved']), 1, '第二张必须留在待归档，不能再挂同一条')

    def test_the_item_ends_up_with_exactly_one_invoice(self):
        mailfetch.collect(
            mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                               password='x', days=0),
            client=FakeClient([
                ('1', build_mail(subject='invoice', filename='a.pdf',
                                 data=money_pdf(22.0, 'A')).as_bytes()),
                ('2', build_mail(subject='invoice', filename='b.pdf',
                                 data=money_pdf(22.0, 'B')).as_bytes()),
            ]))
        self.assertEqual(len(repo.invoices_of_item(self.iid)), 1)


class TestTheInvoiceAmountIsTheFaceAmount(TempDataTest):
    """一票多条时，登记进 invoices 的金额必须是**票面总额**。

    以前用的是主条目的钱（34），背面「价税合计」就印成 34 ——
    可这张票实际是 80，拿去对账会对不上。
    """

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.rid = repo.create_round('十月采购')

    def add(self, name, paid):
        return repo.add_item(self.rid, self.mid, {
            'name': name, 'qty': 1, 'unit': '个', 'price': paid,
            'purpose': 'x', 'applicant': '张三', 'paid': paid})

    def test_it_uses_the_face_amount(self):
        self.add('热缩管', 34.0)
        self.add('面板航插', 46.0)
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0)
        mailfetch.collect(cfg, client=FakeClient(
            [('1', build_mail(subject='发票', filename='inv.pdf',
                              data=money_pdf(80.0)).as_bytes())]))
        inv = repo.list_invoices(round_id=self.rid)[0]
        self.assertEqual(inv['amount'], 80.0, '背面要印票面总额，不是主条目的 34')
