# -*- coding: utf-8 -*-
"""把邮箱收下来的发票附件落盘，并登记到「待归档」区。

顺序：读配置 → 收信（`mailbox`）→ 邮件级去重（UID）→ 附件级去重（内容 sha1）→
落到 `data/mail_inbox/` → 写 `mail_inbox` 表。

**不自动挂到条目** —— 交给人在「待归档」列表里一键挂。
猜错挂到别的采购上，比手工点一下麻烦得多。
"""
import re
from pathlib import Path

from app import config
from app.core import invoicing, mailbox, repo, rules


def collect(cfg=None, client=None, on_log=None):
    """收一次信。

    client 为 None 时连真实邮箱；测试可注入替身。
    返回统计 dict：new / saved / duplicates / skipped / errors。
    """
    cfg = cfg or mailbox.load_config()
    log = on_log or (lambda _m: None)

    result = {'ok': False, 'message': '', 'scanned': 0, 'new': 0,
              'saved': [], 'duplicates': [], 'skipped': [], 'errors': []}

    if not cfg.ready:
        result['message'] = '还没配置邮箱（服务器与账号是必填的）'
        return result

    # 邮件级去重：已经收过的 UID 不再处理
    seen_uids = repo.mail_seen_uids()
    known_sha1 = repo.mail_known_sha1()

    got = mailbox.fetch_attachments(cfg, client=client,
                                    skip_uids=sorted(seen_uids), on_log=log)
    result['ok'] = got['ok']
    result['message'] = got['message']
    result['scanned'] = got['scanned']
    result['skipped'] = got['skipped']
    result['errors'] = list(got['errors'])
    if not got['ok']:
        return result

    config.ensure_dirs()
    folder = Path(config.MAIL_INBOX_DIR)
    folder.mkdir(parents=True, exist_ok=True)

    for att in got['attachments']:
        # 附件级去重：同一张发票被转发多次也只留一份
        if att['sha1'] in known_sha1:
            result['duplicates'].append({'filename': att['filename'], 'sha1': att['sha1']})
            log('跳过重复附件：%s' % att['filename'])
            continue

        try:
            target, state = invoicing.unique_target(folder, att['filename'], att['data'])
            if state == 'new':
                with open(str(target), 'wb') as fh:
                    fh.write(att['data'])
        except Exception as exc:
            result['errors'].append('保存「%s」失败：%s' % (att['filename'], exc))
            continue

        mid = repo.add_mail_attachment(
            att['uid'], att['subject'], att['sender'], att['date'],
            att['filename'], target.name,
            target.relative_to(config.DATA_DIR).as_posix(),
            target.stat().st_size, att['sha1'])
        known_sha1.add(att['sha1'])
        result['new'] += 1
        result['saved'].append({
            'id': mid, 'filename': att['filename'], 'stored_name': target.name,
            'subject': att['subject'], 'sender': att['sender'], 'state': state,
        })
    return result


def report_lines(result):
    """把收信结果整理成几行可读文本（给界面和日志用）。"""
    lines = []
    if not result['ok']:
        lines.append(result['message'] or '收取失败')
        return lines
    lines.append('扫了 %d 封邮件，新收到 %d 张发票' % (result['scanned'], result['new']))
    if result['duplicates']:
        lines.append('重复附件 %d 个（没重复存）：%s'
                     % (len(result['duplicates']),
                        '、'.join(d['filename'] for d in result['duplicates'][:5])))
    if result['skipped']:
        lines.append('跳过 %d 封（已收过或没有发票附件）' % len(result['skipped']))
    for err in result['errors']:
        lines.append('出错：%s' % err)
    return lines


# ------------------------------------------------------------------ 待归档与挂载

def extract_amounts(text) -> list:
    """从文本里抠出可能是金额的数字。

    只认两种：带 ¥/￥ 的，以及带两位小数的（发票金额几乎总是 x.xx）。
    纯整数太容易误伤（数量、型号、订单号都是整数）。
    """
    found = []
    s = text or ''
    for m in re.finditer(r'[¥￥]\s*(\d+(?:\.\d{1,2})?)', s):
        found.append(round(float(m.group(1)), 2))
    # 不带货币符号、但带两位小数的；显式排除前面已经有 ¥ 的情况，避免同一个数字被抓两次
    for m in re.finditer(r'(?<![\d.¥￥])(\d+\.\d{2})(?![\d])', s):
        found.append(round(float(m.group(1)), 2))

    out = []
    for v in found:
        if v not in out:
            out.append(v)
    return out


def suggest_items(mail_row, items, limit=3) -> list:
    """给出「这张发票可能是哪一笔」的候选，供人一键确认。

    只是建议：金额对得上、名称或商家出现在主题里就算线索。
    界面会显示理由，由人拍板 —— 不自动挂。
    """
    haystack = '%s %s %s' % (mail_row.get('subject') or '',
                             mail_row.get('sender') or '',
                             mail_row.get('stored_name') or '',
                             )
    amounts = extract_amounts(haystack)

    scored = []
    for it in items:
        score = 0.0
        reasons = []

        for amt in amounts:
            hit = None
            for fieldname, label in (('paid', '实付'), ('total', '合计')):
                v = rules.to_num(it.get(fieldname))
                if v is None:
                    continue
                if abs(v - amt) < 0.005:
                    hit = (fieldname, label, v)
                    break
            if hit:
                score += 10
                reasons.append('%s金额 %s 与附件里的数字一致' % (hit[1], rules.r2(hit[2])))
                break

        name = (it.get('name') or '').strip()
        if name and name in haystack:
            score += 6
            reasons.append('出现了「%s」' % name)

        supplier = (it.get('url') or '').strip()
        if supplier and not supplier.startswith('http') and supplier in haystack:
            score += 4
            reasons.append('出现了商家「%s」' % supplier)

        if score > 0:
            scored.append({
                'item_id': it['id'], 'name': name, 'applicant': it.get('applicant') or '',
                'paid': it.get('paid'), 'total': it.get('total'),
                'group_name': it.get('group_name') or '',
                'score': score, 'reason': '；'.join(reasons),
            })

    scored.sort(key=lambda x: (-x['score'], x['item_id']))
    return scored[:limit]


def list_pending(round_id=None) -> list:
    """待归档列表；给了 round_id 就带上「可能对应哪一笔」的建议。

    只对**还没挂过**的（pending）给建议；已挂只是留个记录。
    """
    items = repo.list_items(round_id) if round_id else []
    out = []
    for row in repo.list_mail_attachments():
        entry = dict(row)
        entry['suggestions'] = (suggest_items(row, items)
                                if (items and row['status'] == repo.MAIL_PENDING) else [])
        out.append(entry)
    return out


def attach_to_item(mail_id, item_id) -> dict:
    """把待归档的发票挂到某个条目。

    按条目重命名归到 `data/invoices/<轮次>/`，登记进 invoices，
    把发票状态推进到「已收到」，然后清掉待归档区那份文件（避免同一张留两份）。
    """
    row = repo.get_mail_attachment(mail_id)
    if not row:
        raise ValueError('这条待归档记录不存在')
    if row['status'] == repo.MAIL_ATTACHED:
        raise ValueError('这张发票已经挂到条目上了')

    item = repo.get_item(item_id)
    if not item:
        raise ValueError('要挂到的条目不存在')
    rnd = repo.get_round(item['round_id'])
    if not rnd:
        raise ValueError('条目所属的轮次不存在')

    src = Path(config.DATA_DIR) / row['rel_path']
    if not src.exists():
        raise ValueError('待归档的发票文件找不到了：%s' % row['rel_path'])
    data = src.read_bytes()

    rec = invoicing.store_invoice(item, rnd['name'],
                                  row['orig_name'] or row['stored_name'], data)
    inv_id = repo.add_invoice(item_id, item['round_id'], rec['orig_name'],
                              rec['stored_name'], rec['rel_path'], rec['size'],
                              amount=invoicing.invoice_amount(item))
    repo.set_item_status(item_id, invoice_status=config.INV_RECEIVED)
    repo.update_mail_attachment(mail_id, status=repo.MAIL_ATTACHED,
                                item_id=item_id, invoice_id=inv_id)
    invoicing.delete_file(row['rel_path'])
    return {
        'invoice_id': inv_id,
        'stored_name': rec['stored_name'],
        'item': repo.get_item(item_id),
        'invoice': repo.get_invoice(inv_id),
        'duplicate_of_existing': rec['state'] == 'duplicate',
    }


def ignore(mail_id) -> bool:
    """这张不是我要的发票 —— 从待归档区撤下（连文件一起删）。"""
    row = repo.get_mail_attachment(mail_id)
    if not row:
        return False
    return repo.delete_mail_attachment(mail_id)
