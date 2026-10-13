# -*- coding: utf-8 -*-
"""一张发票挂多个条目（一票多条）+ 随时改挂。

淘宝一个订单买好几样、商家只开一张票 —— 这是常态。
以前发票只能挂一条，另外几条永远显示「未要」，「缺发票」清单永远清不掉；
而如果给每条都复制一份发票，合并打印又会把同一张票印好几遍。
"""
import unittest
from pathlib import Path

from app import config
from app.core import db, repo
from app.core.db import session
from tests.support import EntryCase, TempDataTest


class TestInvoiceItems(TempDataTest):

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('八月采购')
        self.mid = repo.add_member('李四', '')
        self.a = self._item('电容 22uF', 22)
        self.b = self._item('AO3400A', 9.24)
        self.c = self._item('电阻包', 33)

    def _item(self, name, price, status='已下单'):
        iid = repo.add_item(self.rid, self.mid, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': '测试', 'applicant': '李四',
        })
        repo.set_item_status(iid, order_status=status)
        return iid

    def _invoice(self, item_id=None, amount=31.24, name='发票.pdf'):
        return repo.add_invoice(item_id, self.rid, name, name,
                                'invoices/' + name, 1000, amount=amount)


class TestSchema(TestInvoiceItems):

    def test_the_link_table_exists(self):
        conn = db.connect()
        with conn:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(invoice_items)')}
        self.assertEqual(cols, {'invoice_id', 'item_id', 'created_at'})

    def test_old_one_to_one_data_is_moved_into_the_link_table(self):
        """迁移要把老数据灌进关联表 —— 否则一票多条的清单会漏掉历史。"""
        inv = self._invoice(self.a)
        with session() as c:
            db.backfill_invoice_items(c)
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.a])

    def test_backfill_is_idempotent(self):
        """跑多少遍都不能重复插（migrate 每次启动都会跑）。"""
        inv = self._invoice(self.a)
        for _ in range(3):
            with session() as c:
                db.backfill_invoice_items(c)
        self.assertEqual(len(repo.items_of_invoice(inv)), 1)


class TestOneInvoiceManyItems(TestInvoiceItems):

    def test_one_invoice_covers_several_items(self):
        inv = self._invoice(self.a, amount=64.24)
        repo.link_invoice(inv, self.b)
        repo.link_invoice(inv, self.c)
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)],
                         [self.a, self.b, self.c])

    def test_linking_twice_does_not_duplicate(self):
        inv = self._invoice(self.a)
        repo.link_invoice(inv, self.b)
        repo.link_invoice(inv, self.b)
        self.assertEqual(len(repo.items_of_invoice(inv)), 2)

    def test_an_item_can_have_several_invoices(self):
        i1 = self._invoice(self.a, amount=10, name='上半张.pdf')
        i2 = self._invoice(self.a, amount=12, name='下半张.pdf')
        self.assertEqual(sorted(x['id'] for x in repo.invoices_of_item(self.a)),
                         sorted([i1, i2]))

    def test_unlink_takes_one_off(self):
        inv = self._invoice(self.a)
        repo.link_invoice(inv, self.b)
        repo.unlink_invoice(inv, self.b)
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.a])


class TestReassign(TestInvoiceItems):
    """改挂：填错了点两下换个目标，不用删票、不用重收邮件。"""

    def test_set_replaces_the_whole_set(self):
        inv = self._invoice(self.a)
        repo.set_invoice_items(inv, [self.b, self.c])
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)],
                         [self.b, self.c])

    def test_set_to_nothing_unlinks_all(self):
        inv = self._invoice(self.a)
        repo.set_invoice_items(inv, [])
        self.assertEqual(repo.items_of_invoice(inv), [])

    def test_the_primary_item_follows(self):
        """老代码还在看 invoices.item_id，得跟着更新，别让两边打架。"""
        inv = self._invoice(self.a)
        repo.set_invoice_items(inv, [self.c, self.b])
        self.assertEqual(repo.get_invoice(inv)['item_id'], self.c)


class TestMissingInvoiceList(TestInvoiceItems):
    """「缺发票」清单以**关联表**为准。

    （这里原来还有两条针对 `items_without_invoice()` 的用例，那函数早已没人调用 ——
    界面和接口用的都是 `items_pending_invoice()`，所以按**在用的那条路**重写。）
    """

    def test_a_shared_invoice_clears_all_of_them(self):
        """一张票盖 3 条，这 3 条都不该再出现在缺票清单里。"""
        inv = self._invoice(self.a, amount=64.24)
        repo.link_invoice(inv, self.b)
        repo.link_invoice(inv, self.c)
        self.assertEqual(repo.items_pending_invoice(self.rid), [])

    def test_refunded_items_are_left_out(self):
        """退了的单发票跟着删了，别一直提醒。"""
        repo.set_refund(self.c, True, 33)
        self.assertNotIn(self.c,
                         [x['id'] for x in repo.items_pending_invoice(self.rid)])


class TestReassignUiWiring(unittest.TestCase):
    """界面上能改挂：每张发票旁边有「改挂」，点了能多选目标条目。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')
        cls.css = (root / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_every_invoice_gets_a_reassign_button(self):
        self.assertIn('function reassignInvoice(', self.js)
        self.assertIn("move.textContent = '改挂'", self.js)

    def test_it_is_a_multi_select_dialog(self):
        self.assertIn('function pickItems(', self.js)
        for needle in ('id="pickBar"', 'id="pickList"', 'id="pickOk"'):
            self.assertIn(needle, self.html, needle)
        self.assertIn('multiple', self.html)
        self.assertIn('#pickList', self.css)

    def test_it_saves_the_new_set(self):
        seg = self.js.split('function reassignInvoice(')[1].split('function ')[0]
        self.assertIn("method: 'PUT'", seg)
        self.assertIn('item_ids', seg)

    def test_it_does_not_use_a_browser_popup(self):
        """跟别处一样的坑：prompt/confirm 在部分内嵌浏览器里不弹。"""
        seg = self.js.split('function pickItems(')[1].split('function ')[0]
        self.assertNotIn('prompt(', seg)
        self.assertNotIn('confirm(', seg)

    def test_reassign_is_shown_to_owners_and_purchase(self):
        """组员对自己那笔也该能改挂（后端只让他挂到自己的条目上）。

        但**不该**对别人的条目显示 —— 那样点了只会 403。
        """
        seg = self.js.split('function invoiceCell(')[1].split('function ')[0]
        self.assertIn('if (canChange) {', seg)
        self.assertLess(seg.index('if (canChange)'),
                        seg.index("move.textContent = '改挂'"))

    def test_a_member_only_sees_his_own_items_in_the_picker(self):
        """列选项时按身份过滤 —— 组员只看得到自己填的。"""
        seg = self.js.split('function reassignInvoice(')[1].split('function ')[0]
        self.assertIn('IS_PURCHASE || isMine(it)', seg)

    def test_the_delete_button_variable_is_defined(self):
        """手滑删掉 `const del = ...` 会让整个发票列在运行时崩掉。

        真删过一次 —— 而且 `node --check` 查不出来（那只是运行时未定义）。
        """
        seg = self.js.split('function invoiceCell(')[1].split('function ')[0]
        self.assertIn("const del = document.createElement('button')", seg)


class TestMemberLimitedReassign(EntryCase):
    """组员能改挂，但**只能动自己的票、只能挂到自己的条目上**。

    这样他自己买的几样能合到一张票上，又碰不到别人的账。
    """

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('八月采购')
        self.mine = self._own('我的电容', self.alice)
        self.mine2 = self._own('我的电阻', self.alice)
        self.theirs = self._own('别人的芯片', self.bob)

    def _own(self, name, member_id):
        iid = repo.add_item(self.rid, member_id, {
            'name': name, 'qty': 1, 'unit': '个', 'price': 10,
            'purpose': '测试', 'applicant': 'x',
        })
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def _ticket(self, item_id, name='票.pdf'):
        return repo.add_invoice(item_id, self.rid, name, name,
                                'invoices/' + name, 100)

    def test_a_member_can_cover_his_own_items(self):
        inv = self._ticket(self.mine)
        r = self.alice_c.put('/api/invoices/%d/items' % inv,
                             json={'item_ids': [self.mine, self.mine2]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(x['id'] for x in repo.items_of_invoice(inv)),
                         sorted([self.mine, self.mine2]))

    def test_a_member_cannot_reach_someone_elses_item(self):
        inv = self._ticket(self.mine)
        r = self.alice_c.put('/api/invoices/%d/items' % inv,
                             json={'item_ids': [self.mine, self.theirs]})
        self.assertEqual(r.status_code, 403, r.text)
        # 而且是整体拒绝，不留半拉子状态
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.mine])

    def test_a_member_cannot_touch_someone_elses_invoice(self):
        inv = self._ticket(self.theirs)
        r = self.alice_c.put('/api/invoices/%d/items' % inv,
                             json={'item_ids': [self.mine]})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.theirs])

    def test_purchase_can_still_do_anything(self):
        inv = self._ticket(self.mine)
        r = self.purchase.put('/api/invoices/%d/items' % inv,
                              json={'item_ids': [self.theirs]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.theirs])

    def test_an_anonymous_caller_is_rejected(self):
        inv = self._ticket(self.mine)
        r = self.anon.put('/api/invoices/%d/items' % inv,
                          json={'item_ids': [self.mine]})
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_a_missing_item_is_a_bad_request(self):
        inv = self._ticket(self.mine)
        r = self.purchase.put('/api/invoices/%d/items' % inv,
                              json={'item_ids': [999999]})
        self.assertEqual(r.status_code, 400, r.text)


class TestInvoicesCarryTheirFullLinks(EntryCase):
    """轮次详情里，每张票要带上**完整归属**。

    只给 `item_id`（主条目）的话，一票多条时**另外几条的发票列会是空的** ——
    界面上表现成"我明明挂了票，那一行却什么都没有"，改挂按钮自然也不出现。
    """

    def test_each_invoice_lists_every_item_it_covers(self):
        rid = repo.create_round('八月采购')
        mid = self.alice
        a = repo.add_item(rid, mid, {'name': '电容', 'qty': 1, 'unit': '个',
                                     'price': 22, 'purpose': 'x', 'applicant': '李四'})
        b = repo.add_item(rid, mid, {'name': '电阻', 'qty': 1, 'unit': '个',
                                     'price': 33, 'purpose': 'x', 'applicant': '李四'})
        inv = repo.add_invoice(a, rid, '票.pdf', '票.pdf', 'invoices/票.pdf', 100)
        repo.link_invoice(inv, b)

        payload = self.purchase.get('/api/rounds/%d' % rid).json()
        row = [v for v in payload['invoices'] if v['id'] == inv][0]
        self.assertEqual(sorted(row['item_ids']), sorted([a, b]))
        # 主条目那个字段也还留着（老代码还在看它）
        self.assertEqual(row['item_id'], a)

    def test_single_attachment_still_reports_one(self):
        rid = repo.create_round('八月采购')
        a = repo.add_item(rid, self.alice, {'name': '电容', 'qty': 1, 'unit': '个',
                                            'price': 22, 'purpose': 'x',
                                            'applicant': '李四'})
        inv = repo.add_invoice(a, rid, '票.pdf', '票.pdf', 'invoices/票.pdf', 100)
        payload = self.purchase.get('/api/rounds/%d' % rid).json()
        row = [v for v in payload['invoices'] if v['id'] == inv][0]
        self.assertEqual(row['item_ids'], [a])


class TestFrontendUsesFullLinks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')

    def test_invoices_of_reads_the_link_list(self):
        seg = self.js.split('function invoicesOf(')[1].split('function ')[0]
        self.assertIn('item_ids', seg)

    def test_it_falls_back_for_old_payloads(self):
        seg = self.js.split('function invoicesOf(')[1].split('function ')[0]
        self.assertIn('v.item_id === itemId', seg)


class TestInvoiceStatusFollowsReality(TestInvoiceItems):
    """有票就该显示「已收到」—— 状态列必须跟事实一致。

    以前只把「主条目」设成已收到，一票多条时另外几条永远显示「未要」，
    界面上就成了「明明有发票，却写着未要」；对账统计也跟着少算。
    """

    def test_linking_marks_the_item_received(self):
        self._invoice(self.a)
        self.assertEqual(repo.get_item(self.a)['invoice_status'],
                         config.INV_RECEIVED)

    def test_every_item_of_a_multi_link_is_marked(self):
        inv = self._invoice(self.a)
        repo.link_invoice(inv, self.b)
        repo.link_invoice(inv, self.c)
        for iid in (self.a, self.b, self.c):
            self.assertEqual(repo.get_item(iid)['invoice_status'],
                             config.INV_RECEIVED, 'item %s' % iid)

    def test_unlinking_the_last_one_goes_back(self):
        inv = self._invoice(self.a)
        repo.unlink_invoice(inv, self.a)
        self.assertEqual(repo.get_item(self.a)['invoice_status'],
                         config.INV_NONE)

    def test_reassign_moves_the_flag_both_ways(self):
        inv = self._invoice(self.a)
        repo.set_invoice_items(inv, [self.b])
        self.assertEqual(repo.get_item(self.a)['invoice_status'], config.INV_NONE)
        self.assertEqual(repo.get_item(self.b)['invoice_status'],
                         config.INV_RECEIVED)

    def test_an_item_with_two_tickets_stays_received_(self):
        self._invoice(self.a, name='上半张.pdf')
        self._invoice(self.a, amount=10, name='下半张.pdf')
        repo.unlink_invoice(repo.invoices_of_item(self.a)[0]['id'], self.a)
        # 还剩一张票，状态不能掉回未要
        self.assertEqual(repo.get_item(self.a)['invoice_status'],
                         config.INV_RECEIVED)

    def test_the_reconciliation_count_is_right(self):
        """对账那句 `invoice_status == 已收到` 的统计，一票多条时要算全。"""
        inv = self._invoice(self.a)
        repo.link_invoice(inv, self.b)
        items = repo.list_items(self.rid)
        got = sum(1 for i in items
                  if i.get('invoice_status') == config.INV_RECEIVED)
        self.assertEqual(got, 2)

    def test_it_still_matches_the_pending_list(self):
        """状态列和「缺发票」清单必须说同一件事，不能一个说有一说没有。"""
        inv = self._invoice(self.a)
        repo.link_invoice(inv, self.b)
        pending = {x['id'] for x in repo.items_pending_invoice(self.rid)}
        marked_none = {i['id'] for i in repo.list_items(self.rid)
                       if i['invoice_status'] != config.INV_RECEIVED}
        self.assertEqual(pending, marked_none)


class TestProgressCountsRefundsOut(EntryCase):
    """发票进度的分母要排除退货的。

    退了的单发票跟着删了，本来就不该有票 —— 留着会让界面显示"还差 5 张"，
    而实际只差 1 张。（真踩过：20 条里 4 条退货，显示 15/20。）
    """

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月')

    def _item(self, name, refunded=False):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': 10,
            'purpose': 'x', 'applicant': '李四',
        })
        repo.set_item_status(iid, order_status='已下单')
        if refunded:
            repo.set_refund(iid, True, 10)
        return iid

    def test_refunded_items_leave_the_denominator(self):
        a = self._item('有票的')
        self._item('退货的', refunded=True)
        self._item('退货的2', refunded=True)
        self._item('真缺票的')
        repo.add_invoice(a, self.rid, 'p.pdf', 'p.pdf', 'invoices/p.pdf', 10)

        p = self.progress()
        self.assertEqual(p['total'], 4)             # 全部条目
        self.assertEqual(p['invoiced_total'], 2)    # 分母只算没退的
        self.assertEqual(p['invoiced'], 1)
        # 于是界面显示 1 / 2 —— "还差一张"，而不是 1 / 4
        self.assertEqual(p['invoiced_total'] - p['invoiced'], 1)

    def test_without_refunds_the_two_numbers_match(self):
        a = self._item('有票的')
        self._item('缺票的')
        repo.add_invoice(a, self.rid, 'p.pdf', 'p.pdf', 'invoices/p.pdf', 10)
        p = self.progress()
        self.assertEqual(p['invoiced_total'], p['total'])

    def progress(self):
        return self.purchase.get('/api/rounds/%d/progress' % self.rid).json()


class TestInvoiceAssign(EntryCase):
    """一票多单：把明细金额分给几条采购（人工核对后提交）。"""

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月')
        self.a = self._item('电容22uF', 22)
        self.b = self._item('电容10uF', 11)
        self.c = self._item('别人的东西', 99, member_id=None)

    def _item(self, name, price, member_id='self'):
        mid = self.alice if member_id == 'self' else self.bob
        iid = repo.add_item(self.rid, mid, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四',
        })
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def _ticket(self, name='票.pdf'):
        return repo.add_invoice(self.a, self.rid, name, name,
                                'invoices/' + name, 100)

    def test_it_splits_the_amounts_across_items(self):
        inv = self._ticket()
        r = self.purchase.post('/api/invoices/%d/assign' % inv, json={'lines': [
            {'item_id': self.a, 'paid': 22.0},
            {'item_id': self.b, 'paid': 11.0},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(x['id'] for x in repo.items_of_invoice(inv)),
                         sorted([self.a, self.b]))
        self.assertEqual(repo.get_item(self.a)['paid'], 22.0)
        self.assertEqual(repo.get_item(self.b)['paid'], 11.0)

    def test_items_left_out_get_unlinked(self):
        """核对时把某行去掉了，那条就该从这张票上摘下来。"""
        inv = self._ticket()
        repo.link_invoice(inv, self.b)
        self.purchase.post('/api/invoices/%d/assign' % inv,
                           json={'lines': [{'item_id': self.a, 'paid': 22.0}]})
        self.assertEqual([x['id'] for x in repo.items_of_invoice(inv)], [self.a])

    def test_an_empty_choice_just_unlinks(self):
        inv = self._ticket()
        r = self.purchase.post('/api/invoices/%d/assign' % inv,
                               json={'lines': []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.items_of_invoice(inv), [])

    def test_it_refuses_an_item_that_does_not_exist(self):
        inv = self._ticket()
        r = self.purchase.post('/api/invoices/%d/assign' % inv,
                               json={'lines': [{'item_id': 999999, 'paid': 1}]})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_member_still_cannot_reach_someone_elses_item(self):
        inv = self._ticket()
        r = self.alice_c.post('/api/invoices/%d/assign' % inv, json={'lines': [
            {'item_id': self.a, 'paid': 22.0},
            {'item_id': self.c, 'paid': 99.0},      # 别人的
        ]})
        self.assertEqual(r.status_code, 403, r.text)

    def test_an_anonymous_caller_is_rejected(self):
        inv = self._ticket()
        r = self.anon.post('/api/invoices/%d/assign' % inv, json={'lines': []})
        self.assertIn(r.status_code, (401, 403), r.text)


class TestProgressBarShowsInvoiceSheets(unittest.TestCase):
    """进度条上「发票」那一格写的是**张数**，不是条目数。

    9 月那轮 20 条采购、实际 13 张票，界面却显示成 20 —— 用户一眼就看出不对
    （原话「应该是 13 张发票才对，现在显示 20 张」）。一票多条时差得更多：
    那张电容票盖了 2 条、AO3400A 票盖了 3 条。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')

    def seg(self):
        return self.js.split('function renderProgress(')[1].split('\nfunction ')[0]

    def test_it_counts_invoice_files(self):
        seg = self.seg()
        self.assertIn('state.invoices || []).length', seg)      # 真·张数
        self.assertIn('张（', seg)
        self.assertNotIn("' / ' + total + '</b> 已收到'", seg)   # 别再拿条目数当张数

    def test_refunded_items_leave_the_denominators(self):
        """退了的条目从「下单 / 到货 / 发票 / 缺金额」四个口径里都出去。"""
        seg = self.seg()
        self.assertIn('i.refunded', seg)
        self.assertIn('退货', seg)                               # 而且要说明白
        self.assertIn('live.length', seg)

    def test_the_covering_count_is_separate(self):
        """张数和「几条采购已经有票」是两件事，得分开写。"""
        self.assertIn('条已有票', self.js)

    def test_the_member_card_shows_sheets_tool(self):
        """组员那张进度卡以前写的是「16 / 16 已收到发票」——

        用户看到就以为有 16 张票（实际 13 张）。现在那格写张数，
        另起一格写「有票的采购」。
        """
        seg = self.js.split('async function refreshMemberProgress(')[1].split('\nfunction ')[0]
        self.assertIn('p.invoice_sheets', seg)
        self.assertIn("' 张'", seg)
        self.assertIn('p.live_total', seg)
        self.assertNotIn("prog('已收到发票', p.invoiced", seg)


class TestProgressCountsSheetsNotItems(EntryCase):
    """接口上的「发票」也要分两个数：**张数** 和 **几条采购有票**。

    一票多条时两者差得很远：电容那张票盖 2 条、AO3400A 那张票盖 3 条，
    所以 13 张票能覆盖 16 条采购。
    """

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月采购')
        self.a = self._item('电容', 22)
        self.b = self._item('电阻', 11)
        self.c = self._item('退掉的', 5, refunded=True)

    def _item(self, name, price, refunded=False):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(iid, order_status='已到货')
        if refunded:
            repo.set_refund(iid, True, price)
        return iid

    def progress(self):
        return self.alice_c.get('/api/rounds/%d/progress' % self.rid).json()

    def test_sheets_count_invoice_files_not_items(self):
        inv = repo.add_invoice(self.a, self.rid, '票.pdf', '票.pdf', 'invoices/票.pdf', 10)
        repo.link_invoice(inv, self.b)              # 一张票盖两条采购
        p = self.progress()
        self.assertEqual(p['invoice_sheets'], 1)    # 票只有 1 张
        self.assertEqual(p['invoiced'], 2)          # 但两条采购都算"有票"
        self.assertEqual(p['live_total'], 2)

    def test_refunds_leave_every_denominator(self):
        p = self.progress()
        self.assertEqual(p['total'], 3)             # 「本轮条目」本身还是 3 条
        self.assertEqual(p['live_total'], 2)        # 但下单 / 发票的分母只算没退的
        self.assertEqual(p['need_order'], 0)
        self.assertEqual(p['need_invoice'], 2)
        self.assertEqual(p['invoice_sheets'], 0)

    def test_the_old_field_still_reads(self):
        """`invoiced_total` 是前端老版本在用的字段，留着兼容。"""
        self.assertEqual(self.progress()['invoiced_total'], 2)


class TestMemberCannotDeleteSomeoneElsesShare(EntryCase):
    """组员删票的权限，跟「改挂」一个规矩：**整张票都挂在自己条目上**才能删。

    以前只看主条目（`invoices.item_id`）—— 一张票盖着 A、B 两条时，
    只要 A 是他的就能把整张删掉，**连带把 B 那条的票也带走**，
    而 B 只会看到"我的票没了"。改挂那边本来就查全部归属，两边得一致。
    """

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('八月采购')
        self.mine = self._own('我的电容', self.alice)
        self.mine2 = self._own('我的电阻', self.alice)
        self.theirs = self._own('别人的芯片', self.bob)

    def _own(self, name, member_id):
        iid = repo.add_item(self.rid, member_id, {
            'name': name, 'qty': 1, 'unit': '个', 'price': 10,
            'purpose': 'x', 'applicant': 'x'})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def _ticket(self, item_id, name='票.pdf'):
        return repo.add_invoice(item_id, self.rid, name, name,
                                'invoices/' + name, 100)

    def test_he_can_delete_his_own_ticket(self):
        inv = self._ticket(self.mine)
        self.assertEqual(self.alice_c.delete('/api/invoices/%d' % inv).status_code, 200)
        self.assertIsNone(repo.get_invoice(inv))

    def test_he_can_delete_one_that_only_covers_his_items(self):
        inv = self._ticket(self.mine)
        repo.link_invoice(inv, self.mine2)          # 两条都是他的
        self.assertEqual(self.alice_c.delete('/api/invoices/%d' % inv).status_code, 200)

    def test_he_cannot_delete_one_that_also_covers_someone_else(self):
        inv = self._ticket(self.mine)               # 主条目是他的
        repo.link_invoice(inv, self.theirs)         # 但还盖着别人的
        r = self.alice_c.delete('/api/invoices/%d' % inv)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNotNone(repo.get_invoice(inv), '别人的票不能被连带删掉')

    def test_purchase_can_still_delete_anything(self):
        inv = self._ticket(self.theirs)
        self.assertEqual(self.purchase.delete('/api/invoices/%d' % inv).status_code, 200)


class TestRowNumbersAndOnlyMine(unittest.TestCase):
    """两个"口径"上的小坑：行号怎么数、「只看我的」按什么筛。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')

    def test_the_row_number_matches_the_reassign_dialog(self):
        """表格第一列和「改挂」弹窗里的 `#N` 得是同一个号。

        以前表格显示"当前视图里的第几个"、弹窗显示"全表第几个"，
        切过视图（缺发票 / 缺金额 / 垫资）之后，同一个 `#5` 不是同一条 ——
        电话里说"第 5 条"就对不上了。
        """
        self.assertIn('function itemNo(', self.js)
        seg = self.js.split('function renderItems(')[1].split('\nfunction ')[0]
        self.assertIn('itemNo(it)', seg)
        self.assertNotIn('idx + 1', seg)
        # 弹窗那边一直用的是全表位置，保持不动
        self.assertIn('state.items.indexOf(it) + 1', self.js)

    def test_only_mine_filters_by_id_not_by_name(self):
        """按姓名文字筛的话，改了名字、或者采购代填就会漏。"""
        seg = self.js.split('function visibleItems(')[1].split('\nfunction ')[0]
        self.assertIn('isMine(i)', seg)
        self.assertNotIn('state.me.name', seg)

    def test_the_search_box_filters_the_round(self):
        """搜索框按关键词筛本轮条目：物料/规格/用途/申购人/备注/链接/垫资人都能搜。"""
        html = (Path(__file__).resolve().parent.parent / 'app/web/index.html').read_text(encoding='utf-8')
        self.assertIn('id="itemSearch"', html)
        seg = self.js.split('function visibleItems(')[1].split('\nfunction ')[0]
        self.assertIn('state.query', seg)
        for field in ('i.name', 'i.spec', 'i.purpose', 'i.applicant',
                      'i.note', 'i.url', 'i.payer'):
            self.assertIn(field, seg)

    def test_several_words_must_all_match(self):
        """空格分开的几个词得**都**命中（「电容 22」一次筛出来）。"""
        seg = self.js.split('function visibleItems(')[1].split('\nfunction ')[0]
        self.assertIn('words.every', seg)

    def test_the_search_box_is_wired_and_escape_clears_it(self):
        self.assertIn("$('itemSearch').addEventListener('input'", self.js)
        self.assertIn("$('itemSearch').addEventListener('keydown'", self.js)
        self.assertIn("'Escape'", self.js)

    def test_it_says_something_when_nothing_matches(self):
        seg = self.js.split('function renderItems(')[1].split('\nfunction ')[0]
        self.assertIn('没搜到', seg)

    def test_the_search_box_has_its_own_style(self):
        css = (Path(__file__).resolve().parent.parent / 'app/web/styles.css').read_text(encoding='utf-8')
        self.assertIn('.searchinp', css)


class TestMultiLinkNaming(EntryCase):
    """一票多条时，归档文件名用**覆盖条目的合计**，不是主条目自己的金额。

    用户问过：`DQ-原装正品AO3400ASOT-23N沟道-9.24.pdf` 那张其实盖了三条
    （9.24 + 7.40 + 5.50 = 22.14），名字里的 9.24 看着像"整张票只值 9.24"。
    """

    def setUp(self):
        super().setUp()
        self.a = self._item('AO3400A', 9.24)
        self.b = self._item('HK4100F', 7.40)
        self.c = self._item('助焊剂', 5.50)
        p = Path(config.INVOICE_DIR) / 'DQ-AO3400A-9.24.pdf'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'%PDF-1.4 fake')
        self.inv = repo.add_invoice(self.a, self.rid, 'x.pdf', 'DQ-AO3400A-9.24.pdf',
                                    'invoices/DQ-AO3400A-9.24.pdf', 10)

    def _item(self, name, price):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四', 'paid': price})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def test_hanging_it_on_more_items_renames_with_the_total(self):
        r = self.purchase.put('/api/invoices/%d/items' % self.inv,
                              json={'item_ids': [self.a, self.b, self.c]})
        self.assertEqual(r.status_code, 200, r.text)
        got = repo.get_invoice(self.inv)
        self.assertEqual(got['stored_name'], 'DQ-AO3400A-22.14.pdf')
        self.assertTrue((Path(config.DATA_DIR) / got['rel_path']).exists(),
                        got['rel_path'])

    def test_taking_it_back_to_one_item_renames_again(self):
        self.purchase.put('/api/invoices/%d/items' % self.inv,
                          json={'item_ids': [self.a, self.b]})
        self.assertEqual(repo.get_invoice(self.inv)['stored_name'], 'DQ-AO3400A-16.64.pdf')
        self.purchase.put('/api/invoices/%d/items' % self.inv,
                          json={'item_ids': [self.a]})
        self.assertEqual(repo.get_invoice(self.inv)['stored_name'], 'DQ-AO3400A-9.24.pdf')


if __name__ == '__main__':
    unittest.main(verbosity=2)
