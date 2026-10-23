# -*- coding: utf-8 -*-
"""数据自检：**库里的记录和磁盘上的文件到底对不对得上**。

为什么需要它：发票是「库里一行 + 磁盘一个 PDF」两边一起记的，
删条目、删发票、手工改库、备份恢复做到一半 —— 都可能让两边错开。
错开之后界面上看不出异常，只是某天打印时突然「文件不见了」。

**只报告，不动手**：删东西的风险太高，先让人看一眼再决定。

跑法：

    python -m app.tools.doctor
"""
import sys
from pathlib import Path

from app import config
from app.core.db import session

# 和 repo.MAIL_ATTACHED 一致；这里不 import repo，免得工具反过来依赖业务模块
ATTACHED = 'attached'


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def check():
    """返回 `[(严重性, 分类, 说明), ...]`；空列表就是没问题。"""
    problems = []
    data = Path(config.DATA_DIR)

    # ---------------- 1. 发票记录 → 磁盘文件
    with session() as c:
        invs = _rows(c.execute(
            'SELECT id, stored_name, rel_path, item_id FROM invoices'))
    for inv in invs:
        if not (data / inv['rel_path']).exists():
            problems.append(('错误', '发票文件丢了',
                             '#%s %s（条目 #%s）'
                             % (inv['id'], inv['stored_name'], inv['item_id'])))

    # ---------------- 2. 磁盘文件 → 发票记录（多余的文件）
    known = {inv['rel_path'] for inv in invs}
    inv_dir = Path(config.INVOICE_DIR)
    if inv_dir.exists():
        for p in sorted(inv_dir.rglob('*')):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(data).as_posix()
            except ValueError:
                continue
            if rel not in known:
                problems.append(('警告', '多余的文件',
                                 '%s（数据库里没有对应记录）' % rel))

    # ---------------- 3. 待归档记录 → 文件
    with session() as c:
        mails = _rows(c.execute(
            'SELECT id, rel_path, status, invoice_id FROM mail_inbox'))
    inv_ids = {inv['id'] for inv in invs}
    for m in mails:
        if m.get('status') == ATTACHED:
            # 已挂的票：文件**已经归档进 invoices/ 了**，`mail_inbox.rel_path`
            # 留的是移走之前的旧位置（历史残留）。按「文件丢了」报是误报 ——
            # 真要查的是它指向的那张发票还在不在（那由第 1 步负责）。
            if not m.get('invoice_id') or m['invoice_id'] not in inv_ids:
                problems.append(('错误', '已挂记录指向不存在的发票',
                                 '#%s（invoice_id=%s）'
                                 % (m['id'], m.get('invoice_id'))))
            continue
        rel = m.get('rel_path')
        if rel and not (data / rel).exists():
            problems.append(('错误', '待归档文件丢了',
                             '#%s %s（状态 %s）' % (m['id'], rel, m['status'])))

    # ---------------- 4. 关联表指向不存在的东西
    with session() as c:
        orphan_items = c.execute(
            'SELECT COUNT(*) FROM invoice_items '
            'WHERE item_id NOT IN (SELECT id FROM items)').fetchone()[0]
        orphan_invs = c.execute(
            'SELECT COUNT(*) FROM invoice_items '
            'WHERE invoice_id NOT IN (SELECT id FROM invoices)').fetchone()[0]
    if orphan_items:
        problems.append(('错误', '关联指向不存在的条目', '%d 行' % orphan_items))
    if orphan_invs:
        problems.append(('错误', '关联指向不存在的发票', '%d 行' % orphan_invs))

    # ---------------- 5. 条目的「发票状态」和实际挂票情况不一致
    # 退货的不看：退货时票本来就跟着删了。
    with session() as c:
        rows = _rows(c.execute(
            'SELECT i.id, i.name, i.invoice_status, '
            '  (SELECT COUNT(*) FROM invoice_items x WHERE x.item_id = i.id) AS n '
            'FROM items i WHERE i.refunded = 0'))
    for r in rows:
        has = (r['n'] or 0) > 0
        says = r['invoice_status'] == config.INV_RECEIVED
        if has and not says:
            problems.append(('警告', '有票但状态没更新',
                             '#%s %s' % (r['id'], r['name'])))
        elif says and not has:
            problems.append(('警告', '状态说有票但实际没挂上',
                             '#%s %s' % (r['id'], r['name'])))

    return problems


def main(argv=None):
    items = check()
    if not items:
        print('检查完了：库里记录和磁盘文件一致，没发现问题。')
        return 0

    wrong = sum(1 for it in items if it[0] == '错误')
    print('检查完了：发现 %d 处问题（其中错误 %d 处）。' % (len(items), wrong))
    print()
    order = {'错误': 0, '警告': 1}
    for level, kind, detail in sorted(items, key=lambda x: (order.get(x[0], 9), x[1])):
        print('  [%s] %s：%s' % (level, kind, detail))
    print()
    print('这个工具只报告、不修改。真要修的话：')
    print('  「文件丢了」      从最近一次备份里找回来（见 使用说明.md 的「备份与恢复」）')
    print('  「多余的文件」    确认没用之后再手工删')
    print('  「状态不一致」    在界面上对该条目「传发票」或「撤下发票」一次就会对齐')
    return 1 if wrong else 0


if __name__ == '__main__':
    sys.exit(main())
