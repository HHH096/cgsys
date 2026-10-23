# -*- coding: utf-8 -*-
"""组员管理：改名、加人、删人。

需求是「名字打错了、有人进队退队要能改」。后端本来就有这三个接口，
缺的是界面；而且原来那个「+ 组员」用的是浏览器原生 `prompt()`，
有些环境根本不弹 —— 这就是「加不了人」的直接原因。
"""
import unittest
from pathlib import Path

from app.core import repo
from tests.support import EntryCase

ROOT = Path(__file__).resolve().parent.parent


class TestMemberEditing(EntryCase):

    def test_rename_a_member(self):
        r = self.purchase.put('/api/members/%d' % self.alice,
                              json={'name': '李四改过', 'phone': '13800000000'})
        self.assertEqual(r.status_code, 200, r.text)
        m = repo.get_member(self.alice)
        self.assertEqual(m['name'], '李四改过')
        self.assertEqual(m['phone'], '13800000000')

    def test_renaming_does_not_touch_existing_items(self):
        """改名不该动已经填过的条目 —— 条目里存的是当时的名字。"""
        iid = self._add(self.alice_c, self.alice, '改名前的条目')
        before = repo.get_item(iid)['applicant']
        self.purchase.put('/api/members/%d' % self.alice,
                          json={'name': '新名字', 'phone': ''})
        self.assertEqual(repo.get_item(iid)['applicant'], before)
        self.assertEqual(repo.get_item(iid)['name'], '改名前的条目')

    def test_delete_a_member(self):
        r = self.purchase.delete('/api/members/%d' % self.bob)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(repo.get_member(self.bob))

    def test_deleting_a_member_keeps_his_items(self):
        iid = self._add(self.bob_c, self.bob, '他填的')
        self.purchase.delete('/api/members/%d' % self.bob)
        self.assertIsNotNone(repo.get_item(iid), '条目不该跟着人一起没')

    def test_add_a_member(self):
        r = self.purchase.post('/api/members', json={'name': '新同学', 'phone': ''})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('新同学', [m['name'] for m in repo.list_members()])

    def test_only_purchase_can_edit_members(self):
        """组员改不了别人的名字，更删不了人。"""
        for label, client in (('匿名', self.anon), ('组员', self.alice_c)):
            self.assertEqual(
                client.put('/api/members/%d' % self.bob,
                           json={'name': 'x'}).status_code, 401, label)
            self.assertEqual(
                client.delete('/api/members/%d' % self.bob).status_code, 401, label)


class TestMemberPanelWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_the_panel_exists(self):
        for needle in ('id="memBody"', 'id="memAdd"', 'id="memHint"', 'id="memTable"'):
            self.assertIn(needle, self.html, needle)

    def test_it_is_purchase_only(self):
        """组员不该看到成员管理。"""
        before = self.html.split('id="memBody"')[0]
        section = before.rsplit('<section', 1)[-1]      # 往前找最近的 section 开头
        self.assertIn('purchase-only', section[:150], section[:150])

    def test_it_can_edit_and_delete(self):
        self.assertIn('function memberRow(', self.js)
        self.assertIn('function renderMembers()', self.js)
        self.assertIn('function saveMemberRow(', self.js)
        self.assertIn('function removeMemberRow(', self.js)
        self.assertIn("method: 'PUT'", self.js)
        self.assertIn("method: 'DELETE'", self.js)

    def test_adding_does_not_use_a_browser_prompt(self):
        """原来那个「+ 组员」用 prompt()，有些环境压根不弹 —— 这就是加不了人的原因。"""
        seg = self.js.split('function addMemberRow()')[1].split('function ')[0]
        self.assertNotIn('prompt(', seg)

    def test_the_row_is_rendered_on_load(self):
        self.assertIn('renderMembers();', self.js)

    def test_new_row_is_highlighted(self):
        self.assertIn('tr.newrow', self.css)

    def test_the_action_column_is_pinned_to_the_right(self):
        """手机上表格要横向滑；不把「存/删」钉住，这一列会被挤出屏幕。

        真事：有同学在手机上填完名字，却怎么都点不到「存」，
        以为系统坏了。
        """
        self.assertIn('#memTable td:last-child', self.css)
        self.assertIn('position: sticky', self.css)

    def test_input_width_is_not_hardcoded_in_js(self):
        """宽度写死在 JS 里会把手机上的表格撑爆，操作列就没影了。"""
        seg = self.js.split('function memberRow(')[1].split('function ')[0]
        self.assertNotIn('style.width', seg)

    def test_the_pending_hint_tells_you_to_click_save(self):
        """光说「先把上面那行填完」不够 —— 人不知道得点「存」。"""
        seg = self.js.split('function addMemberRow()')[1].split('function ')[0]
        # 只看真代码，注释里提到旧提示语不算
        code = '\n'.join(
            l for l in seg.splitlines() if not l.strip().startswith('//'))
        self.assertIn('存', code)
        self.assertNotIn('先把上面那行填完', code)


class TestNoBrowserPrompt(unittest.TestCase):
    """prompt() 在部分内嵌浏览器（微信等）里不弹 —— 点了像没反应。

    所以整个前端一个都不该留。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_no_browser_prompt_is_left_anywhere(self):
        self.assertNotIn('prompt(', self.js)

    def test_shared_invoices_get_a_group_colour(self):
        """一张票覆盖多条时，那几条涂同一个淡色，不同票不同色。"""
        self.assertIn('function invoiceGroupColors(', self.js)
        seg = self.js.split('function invoiceGroupColors(')[1].split('function ')[0]
        self.assertIn('item_ids', seg)          # 按完整归属分组
        self.assertIn('ids.length < 2', seg)    # 一票一条不上色
        self.assertIn('INV_GROUP_COLORS', self.js)

    def test_the_colours_are_pale(self):
        """要淡 —— 抢了内容就得不偿失。"""
        import re as _re
        block = self.js.split('const INV_GROUP_COLORS = [')[1].split(']')[0]
        hexes = _re.findall(r'#[0-9a-fA-F]{6}', block)
        self.assertEqual(len(hexes), 8, '应该有 8 个颜色')
        for h in hexes:
            r_, g_, b_ = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreater(min(r_, g_, b_), 220,
                               '%s 太深了，要淡色' % h)

    def test_the_row_paints_itself(self):
        seg = self.js.split('function renderItems(')[1].split('function ')[0]
        self.assertIn('invoiceGroupColors()', seg)
        self.assertIn('tr.style.background', seg)

    def test_the_view_total_lives_outside_the_table(self):
        """「当前视图合计」不能跟数据行连成一片 —— 会看着像表格还有一条。

        以前它是 `<tfoot>`（长在表格里），现在挪到表格外面独立一块。
        """
        self.assertNotIn('itemsFoot', self.html)
        self.assertNotIn('itemsFoot', self.js)
        self.assertNotIn('<tfoot', self.html)
        self.assertIn('id="itemsSum"', self.html)
        self.assertIn('.itemsum', self.css)
        # 它得在 </table> **之后**，不能在里面
        self.assertIn('</table>', self.html.split('id="itemsSum"')[0])

    def test_the_total_shows_a_count(self):
        """顺带把"这几条"的条数写出来，免得看不出来合计的是哪一批。"""
        seg = self.js.split('function renderItems(')[1].split('function ')[0]
        self.assertIn('当前视图合计（', seg)

    def test_the_total_hides_when_there_is_no_data(self):
        """空的时候不能留一条空黄带 —— `display:flex` 会盖掉 [hidden]。"""
        self.assertIn('.itemsum[hidden]', self.css)

    def test_the_name_gate_blocks_until_you_pick(self):
        """**不选名字不让用** —— 申购人空着的采购等于没主。

        以前只在点「保存条目」时才提示一句，那会儿人已经填完一整条了；
        而且不选也能在页面里随便点。
        """
        for needle in ('id="nameGate"', 'id="nameGateSel"', 'id="nameGateOk"'):
            self.assertIn(needle, self.html, needle)
        self.assertIn('function showNameGate(', self.js)
        self.assertIn('function pickMyName(', self.js)
        self.assertIn('function ensureMyName(', self.js)
        # 组员入口加载完，必须检查名字（认过就过、没认过就弹门）
        seg = self.js.split('async function startMember(')[1].split('\nfunction ')[0]
        self.assertIn('ensureMyName()', seg)

    def test_the_chosen_name_is_remembered(self):
        """认一次就记住，别每次打开都问。"""
        self.assertIn("localStorage.setItem(NAME_KEY", self.js)
        self.assertIn("localStorage.getItem(NAME_KEY", self.js)

    def test_the_page_input_exists(self):
        self.assertIn('function askText(', self.js)
        for needle in ('id="askBar"', 'id="askInput"', 'id="askOk"', 'id="askCancel"'):
            self.assertIn(needle, self.html, needle)
        self.assertIn('.askbar', self.css)

    def test_editing_scrolls_the_form_into_view(self):
        """手机上条目表在下面，点了「改」不滚上来，看着像没反应。"""
        seg = self.js.split('function startEdit(')[1].split('function ')[0]
        self.assertIn('scrollIntoView', seg)

    def test_save_failure_points_at_the_missing_field(self):
        """光说「用途 不能为空」还得自己一个个找，应该直接跳到那个框。"""
        self.assertIn(':invalid', self.js)

    def test_long_urls_are_shortened_into_a_clickable_link(self):
        """淘宝分享的链接有 100 多字符，铺进表格会把整张表撑爆。"""
        self.assertIn('function shortUrl(', self.js)
        seg = self.js.split('if (i === 11 && it.url)')[1].split('tr.appendChild')[0]
        self.assertIn("target = '_blank'", seg)     # 点了在新标签页打开
        self.assertIn('shortUrl(url)', seg)         # 显示短标签
        self.assertIn('a.title = url', seg)         # 悬浮能看完整地址

    def test_only_http_links_become_a_real_link(self):
        """组员能往「网址链接」里塞 `javascript:` —— 那种只当纯文本显示，不给 <a>。

        否则采购一点开，等于在自己电脑上执行组员写的脚本。
        """
        seg = self.js.split('if (i === 11 && it.url)')[1].split('tr.appendChild')[0]
        self.assertIn('/^https?:\\/\\//i.test(url)', seg)
        self.assertIn('td.textContent = url', seg)   # 不是 http(s) 就走纯文本


if __name__ == '__main__':
    unittest.main(verbosity=2)
