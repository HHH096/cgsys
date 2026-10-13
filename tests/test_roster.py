# -*- coding: utf-8 -*-
"""P2 组员名单预置。"""
import unittest

from app import config
from app.core import repo, roster
from tests.support import TempDataTest


class TestParse(unittest.TestCase):

    def test_basic_lines(self):
        people, bad = roster.parse_roster('张三,13800000000\n李四,15100000000\n')
        self.assertEqual(people, [('张三', '13800000000', False),
                                  ('李四', '15100000000', False)])
        self.assertEqual(bad, [])

    def test_comments_and_blank_lines_ignored(self):
        people, bad = roster.parse_roster(
            '# 这是注释\n\n   \n张三,13800000000\n#又一行注释\n')
        self.assertEqual(people, [('张三', '13800000000', False)])
        self.assertEqual(bad, [])

    def test_phone_may_be_missing(self):
        people, _ = roster.parse_roster('王五\n')
        self.assertEqual(people, [('王五', '', False)])

    def test_owner_by_leading_star(self):
        people, _ = roster.parse_roster('*张三,13800000000\n')
        self.assertEqual(people, [('张三', '13800000000', True)])

    def test_owner_by_fullwidth_star(self):
        people, _ = roster.parse_roster('＊张三\n')
        self.assertEqual(people, [('张三', '', True)])

    def test_owner_by_third_column(self):
        people, _ = roster.parse_roster('张三,13800000000,负责人\n')
        self.assertEqual(people, [('张三', '13800000000', True)])

    def test_all_separators(self):
        for line in ('张三,13800000000', '张三，13800000000',
                     '张三\t13800000000', '张三 13800000000'):
            people, _ = roster.parse_roster(line)
            self.assertEqual(people, [('张三', '13800000000', False)], line)

    def test_name_glued_to_phone_is_split(self):
        """从别处复制名单时很容易漏掉分隔符。

        真实遇到的一行：「王五13900000000」——
        原来会把整串当成姓名，于是库里多出一个叫这个名字的人。
        """
        people, bad = roster.parse_roster('王五13900000000')
        self.assertEqual(people, [('王五', '13900000000', False)])
        self.assertEqual(bad, [])

    def test_glued_phone_works_with_the_owner_star(self):
        people, _ = roster.parse_roster('*王五13900000000')
        self.assertEqual(people, [('王五', '13900000000', True)])

    def test_written_correctly_is_left_alone(self):
        """本来就写对的，别被这条规则搅乱。"""
        people, _ = roster.parse_roster('王2号,13800138000')
        self.assertEqual(people, [('王2号', '13800138000', False)])

    def test_a_bare_number_stays_a_name(self):
        """纯数字不当「姓名+手机号」拆 —— 姓名那截必须以非数字结尾。"""
        people, _ = roster.parse_roster('13800138000')
        self.assertEqual(people, [('13800138000', '', False)])

    def test_bad_phone_is_reported_not_silently_dropped(self):
        people, bad = roster.parse_roster('张三,abc\n李四,15100000000\n')
        self.assertEqual([p[0] for p in people], ['李四'])
        self.assertEqual(bad[0]['line'], 1)
        self.assertIn('手机号', bad[0]['why'])


class TestImport(TempDataTest):

    def write(self, text):
        config.ROSTER_FILE.write_text(text, encoding='utf-8')

    def test_missing_file_is_not_an_error(self):
        s = roster.import_roster()
        self.assertFalse(s['exists'])
        self.assertEqual(s['added'], [])
        self.assertEqual(repo.list_members(), [])

    def test_adds_people(self):
        self.write('# 名单\n*张三,13800000000\n李四,15100000000\n王五\n')
        s = roster.import_roster()
        self.assertEqual(s['added'], ['张三', '李四', '王五'])
        got = repo.list_members()
        self.assertEqual([m['name'] for m in got], ['张三', '李四', '王五'])
        self.assertEqual(got[0]['phone'], '13800000000')
        self.assertEqual(got[0]['is_owner'], 1)
        self.assertEqual(got[2]['phone'], '')

    def test_import_is_idempotent(self):
        self.write('张三,13800000000\n')
        self.assertEqual(roster.import_roster()['added'], ['张三'])
        second = roster.import_roster()
        self.assertEqual(second['added'], [])
        self.assertEqual(len(repo.list_members()), 1)

    def test_existing_name_is_not_renamed_or_cleared(self):
        """已在系统里手工加过的人：不会被改名，手机号也不会被清空。"""
        repo.add_member('张三', '19900000000')
        self.write('张三,13800000000\n')
        s = roster.import_roster()
        self.assertEqual(s['added'], [])
        self.assertEqual(s['updated'], [], '已有手机号就不该动')
        self.assertEqual(repo.list_members()[0]['phone'], '19900000000')

    def test_empty_phone_gets_filled(self):
        repo.add_member('李四')
        self.write('李四,15100000000\n')
        s = roster.import_roster()
        self.assertEqual(s['updated'], ['李四'])
        self.assertEqual(repo.list_members()[0]['phone'], '15100000000')

    def test_the_owner_star_only_applies_to_new_people(self):
        """名单里的 `*` 只管"第一次把人加进来"，**不覆盖已有的人**。

        以前这里会「补负责人标记」，于是每重启一次服务，用户在「组员管理」里
        取消掉的负责人就被重新设回来 —— 线上真发生过（用户报"老是取消不了"）。
        """
        repo.add_member('张三')                     # 先有人，而且不是负责人
        self.write('*张三,13800000000\n')
        roster.import_roster()
        self.assertEqual(repo.list_members()[0]['is_owner'], 0,
                         '已经存在的人不该被名单改成负责人')
        # 手机号还是照补（那是"补齐资料"，不是"替人做决定"）
        self.assertEqual(repo.list_members()[0]['phone'], '13800000000')

    def test_a_new_person_still_gets_the_star(self):
        self.write('*李四,15100000000\n')
        roster.import_roster()
        self.assertEqual(repo.list_members()[0]['is_owner'], 1)

    def test_duplicate_in_file_is_reported_once(self):
        self.write('张三,13800000000\n张三,13800000000\n')
        s = roster.import_roster()
        self.assertEqual(s['added'], ['张三'])
        self.assertEqual(len(repo.list_members()), 1)
        self.assertTrue(any('重复' in b['why'] for b in s['bad']))

    def test_bom_and_crlf_are_handled(self):
        """用记事本保存会带 BOM 和 CRLF，不能把第一行吃掉或把名字带上 \\r。"""
        config.ROSTER_FILE.write_bytes('\ufeff张三,13800000000\r\n李四\r\n'.encode('utf-8'))
        s = roster.import_roster()
        self.assertEqual(s['added'], ['张三', '李四'])

    def test_file_path_is_reported(self):
        self.write('张三\n')
        self.assertEqual(roster.import_roster()['file'], str(config.ROSTER_FILE))


class TestShippedRosterFile(unittest.TestCase):
    """仓库里那份 组员名单.txt 是交给用户自己填的模板。

    原来这里断言「文件里一个名字都不能有」，是为了防开发时把**测试用的假名字**
    留在交付文件里。但你正式填上自己队里的名单之后，那条断言就必然失败 ——
    它分不清「开发留下的脏数据」和「用户自己的人」。

    所以换成两条真正有意义的：格式能解析、且没有明显的占位名字。
    """

    PLACEHOLDERS = ('张三', '李四', '王五', '测试', '例子', 'xxx', 'todo', 'placeholder')

    @staticmethod
    def _path():
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / '组员名单.txt'

    def _text(self):
        p = self._path()
        self.assertTrue(p.is_file(), '缺 组员名单.txt')
        return p.read_text(encoding='utf-8-sig')

    def test_parses_without_complaints(self):
        """每行都得能读懂 —— 少个逗号这种手误要当场暴露出来。"""
        people, bad = roster.parse_roster(self._text())
        self.assertEqual(bad, [], '名单里有读不懂的行：%s' % (bad,))
        for row in people:
            self.assertTrue(str(row[0]).strip(), '有个名字是空的：%r' % (row,))

    def test_no_developer_placeholders(self):
        """别把开发时的占位名字留在交付文件里。"""
        people, _ = roster.parse_roster(self._text())
        for name in [str(r[0]) for r in people]:
            for junk in self.PLACEHOLDERS:
                self.assertNotIn(junk, name.lower(),
                                 '名单里出现了占位名字「%s」' % name)

    def test_shipped_file_explains_the_format(self):
        text = self._text()
        self.assertIn('姓名', text)
        self.assertIn('负责人', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
