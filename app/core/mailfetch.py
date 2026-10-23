# -*- coding: utf-8 -*-
"""把邮箱收下来的发票附件落盘，并登记到「待归档」区。

顺序：读配置 → 收信（`mailbox`）→ 邮件级去重（UID）→ 附件级去重（内容 sha1）→
落到 `data/mail_inbox/` → 写 `mail_inbox` 表。

**不自动挂到条目** —— 交给人在「待归档」列表里一键挂。
猜错挂到别的采购上，比手工点一下麻烦得多。
"""
import re
import threading
from itertools import combinations
from pathlib import Path

from app import config
from app.core import invoicing, mailbox, repo, rules
from app.core.db import session as db_session

# 同一时刻只允许一次收信在跑（见 collect 的说明）
_collect_lock = threading.Lock()


def collect(cfg=None, client=None, on_log=None):
    """收一次信 —— **同一时刻只允许一个在跑**。

    后台自动收信（定时）和人工点「立即收取」可能同时发生：
    两边都读到「这封还没收过」，于是各存一份。两条记录共用同一个 `rel_path`，
    其中一条被忽略/挂载后文件被删，另一条点开就是 404。

    这里用进程内锁串行化：后来的那次等前一次结束，再靠 UID 去重
    自动跳过刚收过的邮件 —— 结果就是不会重复入库。
    """
    with _collect_lock:
        return _collect_impl(cfg=cfg, client=client, on_log=on_log)


def _collect_impl(cfg=None, client=None, on_log=None):
    """收一次信。

    client 为 None 时连真实邮箱；测试可注入替身。
    返回统计 dict：new / saved / duplicates / skipped / errors。
    """
    cfg = cfg or mailbox.load_config()
    log = on_log or (lambda _m: None)

    result = {'ok': False, 'message': '', 'scanned': 0, 'new': 0,
              'saved': [], 'duplicates': [], 'skipped': [], 'errors': [],
              'auto_attached': []}

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

    # 收完信：把「只可能属于一条采购」的票直接挂上（可在邮箱设置里关掉）。
    # 挂不上的就留在待归档，和以前一样由人来点。
    if getattr(cfg, 'auto_attach', True) and result['saved']:
        for item in list(result['saved']):
            # **每张票都重算候选池**：上一张挂走的条目已经有票了，
            # 不能再被这一张挂上去。复用循环外算的那一份 pool 会导致
            # 「同一次收信，一条采购挂两张票」—— 重复报销，而且事后极难发现。
            try:
                pool = all_pending_invoice_items()
            except Exception as exc:
                result['errors'].append('自动挂载没跑起来：%s' % exc)
                break
            if not pool:
                break
            try:
                got = auto_attach(item['id'], pool)
            except Exception as exc:
                result['errors'].append('自动挂载「%s」失败：%s'
                                        % (item['filename'], exc))
                continue
            if not got:
                continue
            result['saved'].remove(item)
            ids = got.get('auto_item_ids') or [got['item']['id']]
            hit = got.get('items') or [got['item']]
            names = '、'.join((x.get('name') or '') for x in hit if x.get('name'))
            result['auto_attached'].append({
                'id': item['id'], 'filename': item['filename'],
                'item_id': got['item']['id'],
                'item_ids': ids,
                'item_name': names or (got['item'].get('name') or ''),
                'count': len(ids),
                'round_id': got['item']['round_id'],
            })
            log('自动挂上：%s → 「%s」（%d 条）' % (item['filename'], names, len(ids)))
    return result


def report_lines(result):
    """把收信结果整理成几行可读文本（给界面和日志用）。"""
    lines = []
    if not result['ok']:
        lines.append(result['message'] or '收取失败')
        return lines
    lines.append('扫了 %d 封邮件，新收到 %d 张发票' % (result['scanned'], result['new']))
    auto = result.get('auto_attached') or []
    if auto:
        lines.append('其中 %d 张**自动挂上了**：%s'
                     % (len(auto),
                        '、'.join('「%s」→ %s%s'
                                  % (a['filename'], a['item_name'],
                                     ('（%d 条）' % a['count'])
                                     if (a.get('count') or 1) > 1 else '')
                                  for a in auto[:5])))
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
    # 符号和数字之间**只能隔空格/制表符，不能跨行**（所以是 [ \t]* 而不是 \\s*）。
    # 为什么：发票把「¥」和金额分列排版，跨行匹配会顺着换行抓到下一行 ——
    # 实测把票面的校验码（19 位整数）当成金额，¥52.05 被读成 5.1e18。
    # 整数位也限死 7 位：金额不会过千万，长数字是账号/校验码。
    for m in re.finditer(r'[¥￥][ \t]*(\d{1,7}(?:\.\d{1,2})?)', s):
        found.append(round(float(m.group(1)), 2))
    # 不带货币符号、但带两位小数的；显式排除前面已经有 ¥ 的情况，避免同一个数字被抓两次
    for m in re.finditer(r'(?<![\d.¥￥])(\d{1,7}\.\d{2})(?![\d])', s):
        found.append(round(float(m.group(1)), 2))

    out = []
    for v in found:
        if v not in out:
            out.append(v)
    return out


_face_cache = {}      # (路径, mtime, 大小) -> 票面金额；读 PDF 比查库慢得多
_FACE_CACHE_MAX = 1024


def _cache_face(key, value):
    """写缓存，**超了按最久未用淘汰**（不是整个清空）。

    以前是 `if len > 500: _face_cache.clear()` —— 待归档一旦超过 500 张，
    下一次刷新就要把所有 PDF 重新解析一遍（每张 30-80ms，500 张就是 15-40 秒），
    然后每次刷新再来一次。清空是最坏的选择。
    """
    if len(_face_cache) >= _FACE_CACHE_MAX and key not in _face_cache:
        # dict 保持插入顺序：扔掉最早插进来的那一个，够用了
        try:
            _face_cache.pop(next(iter(_face_cache)))
        except (StopIteration, KeyError):
            pass
    _face_cache[key] = value


def _invoice_face_amount(mail_row):
    """只读**票面**上的金额（元数据里的不算）。读不出返回 None。

    带进程内缓存：`GET /api/mail/pending` 会把每条待归档的票都读一遍，
    票多了每次刷新都重解开一遍 PDF。文件名+修改时间+大小 变了才重读。
    """
    rel = (mail_row.get('rel_path') or '').strip()
    if not rel:
        return None
    path = Path(config.DATA_DIR) / rel
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _face_cache:
        return _face_cache[key]

    from app.core import invoicepdf
    try:
        got = invoicepdf.read_amount(path)
    except Exception:
        got = None
    _cache_face(key, got)
    return got


def _mail_amounts(mail_row):
    """这张邮件相关的金额：元数据里的 **加上票面文字层里的**。

    为什么必须读票面：真实邮件的主题常常是「李锐涛发票10月」，文件名是发票号
    （`dzfp_26412000004041153361_xxx.pdf`），两处都没有金额 —— 金额只在 PDF 里面。
    只从元数据抠数字的话候选恒为 0，自动挂就永远不会触发（实测 27 张票全是 0 候选）。

    照片/扫描件没有文字层，`read_amount` 会返回 None，那就只剩元数据这条路 ——
    这类票本来就该走人工确认。
    """
    haystack = '%s %s %s' % (mail_row.get('subject') or '',
                             mail_row.get('sender') or '',
                             mail_row.get('stored_name') or '')
    amounts = list(extract_amounts(haystack))
    got = _invoice_face_amount(mail_row)
    if got is not None and got not in amounts:
        amounts.append(got)
    return amounts


def suggest_items(mail_row, items, limit=3) -> list:
    """给出「这张发票可能是哪一笔」的候选，供人一键确认。

    只是建议：金额对得上、名称或商家出现在主题里就算线索。
    界面会显示理由，由人拍板。

    金额优先取**票面**（见 `_mail_amounts`）—— 只看邮件主题/文件名等于看不出金额。
    """
    haystack = '%s %s %s' % (mail_row.get('subject') or '',
                             mail_row.get('sender') or '',
                             mail_row.get('stored_name') or '',
                             )
    amounts = _mail_amounts(mail_row)

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


# 自动挂载的门槛：只有「票面金额和某一条的实付/合计精确对上」才算数。
# suggest_items 里金额命中是 +10、名称 +6、商家 +4 —— 所以 10 分意味着「金额对上了」。
AUTO_ATTACH_MIN_SCORE = 10


def all_pending_invoice_items():
    """所有轮次里「还没收到发票」的条目 —— 自动挂载只在这些里面找。

    范围卡在「还缺票的」有两个好处：已经挂过票的条目不会被挂第二张（一票多条
    那种本来就该人来判断），候选池也小得多，误挂概率更低。
    """
    out = []
    for r in repo.list_rounds():
        out.extend(repo.items_pending_invoice(r['id']))
    return out


def auto_attach(mail_id, items=None):
    """自动挂：**单条能确定**就挂单条，**几条加起来正好**就挂多条；有歧义就不动。

    为什么门槛定得这么死：**挂错比不挂糟得多** ——
    票挂到错条目上，对账、合并打印、报销全跟着错，而且很难发现。

    两条路，都要求「唯一」：

    ① **单条**：只有一个候选，且分数 ≥ `AUTO_ATTACH_MIN_SCORE`
       （即票面金额精确匹配）。名称/商家命中（6/4 分）不作数。
    ② **一票多条**：票面金额正好等于几笔之和（2~3 条，同轮次），
       且**只有一种拼法**。淘宝一个订单买几样、商家只开一张票就是这样。

    单条优先：一条就对得上时不去凑组合 —— 单条是更强的证据。

    照片/扫描件读不出金额时两条路都走不通，自然不挂（这是对的）。
    """
    row = repo.get_mail_attachment(mail_id)
    if not row or row['status'] != repo.MAIL_PENDING:
        return None
    pool = all_pending_invoice_items() if items is None else items
    if not pool:
        return None

    # ① 单条：唯一候选 + 金额精确对上
    cands = suggest_items(row, pool, limit=2)
    if len(cands) == 1 and cands[0]['score'] >= AUTO_ATTACH_MIN_SCORE:
        got = attach_to_item(mail_id, cands[0]['item_id'], auto=True)
        got['auto_item_ids'] = [cands[0]['item_id']]
        return got

    # ② 一票多条：只看**票面**金额（元数据里的数字不是票面金额）
    face = _invoice_face_amount(row)
    if face is None:
        return None
    # 按组合大小**从小到大**试：2 条唯一就可以定了，别再去试 3 条。
    # 实测条目里一堆十几二十块的小额，3 条凑数几乎必然出现大量巧合
    # （¥192 用 2 条只有 1 种拼法，用 3 条有 42 种）—— 一起找会把确定答案冲掉。
    for k in range(2, MAX_COMBO_ITEMS + 1):
        # stop_after=2：只要知道「是不是唯一」，找到第二种就能下结论了
        combos = sum_combinations(face, pool, min_items=k, max_items=k, stop_after=2)
        if len(combos) == 1:
            ids = [it['id'] for it in combos[0]]
            got = attach_to_items(mail_id, ids, auto=True)
            got['auto_item_ids'] = ids
            return got
        if len(combos) > 1:
            return None            # 这个大小就有歧义，再往大找只会更歧义
    return None


def list_pending(round_id=None) -> list:
    """待归档列表；给了 round_id 就带上「可能对应哪一笔」的建议。

    只对**还没挂过**的（pending）给建议；已挂只是留个记录。
    """
    items = repo.list_items(round_id) if round_id else []
    # 一次查完所有发票的归属 —— 以前在循环里逐条 items_of_invoice，
    # 200 张已挂就是 200 次连接，而每 20 秒自动刷新都要重跑。
    links = repo.links_map()
    out = []
    for row in repo.list_mail_attachments():
        entry = dict(row)
        entry['suggestions'] = (suggest_items(row, items)
                                if (items and row['status'] == repo.MAIL_PENDING) else [])
        # 已挂的带上「一共挂到了哪几条」—— 一票多条时只显示主条目会让人以为挂错了
        if row['status'] == repo.MAIL_ATTACHED and row.get('invoice_id'):
            ids = links.get(row['invoice_id']) or []
            entry['item_ids'] = ids
            entry['item_count'] = len(ids)
        out.append(entry)
    return out


def attach_to_item(mail_id, item_id, auto=False) -> dict:
    """把待归档的发票挂到某个条目。

    按条目重命名归到 `data/invoices/<轮次>/`，登记进 invoices，
    把发票状态推进到「已收到」，然后清掉待归档区那份文件（避免同一张留两份）。

    `auto=True` 表示这是系统自己判断后挂的（见 `auto_attach`）——
    会记进 `auto_attached` 列，界面上要能一眼看出「人没点过它」。
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
        # 并发：别人刚把这票挂走，文件已经被移进 `invoices/` 了。
        # 这时直接说「文件找不到了」会让人以为文件坏了 —— 先回查一次状态。
        again = repo.get_mail_attachment(mail_id)
        if again and again['status'] == repo.MAIL_ATTACHED:
            raise ValueError('这张发票已经挂到条目上了（可能是别人刚挂的）')
        raise ValueError('待归档的发票文件找不到了：%s' % row['rel_path'])
    data = src.read_bytes()

    rec = invoicing.store_invoice(item, rnd['name'],
                                  row['orig_name'] or row['stored_name'], data)
    # 登记 + 挂载 + 改待归档状态 **一个事务**：分开提交的话，中途失败会留下
    # 「票登记了、待归档还显示没挂」→ 人再点一次就重复登记（同一文件两条记录）。
    with db_session() as c:
        inv_id = repo.add_invoice(item_id, item['round_id'], rec['orig_name'],
                                  rec['stored_name'], rec['rel_path'], rec['size'],
                                  conn=c, amount=invoicing.invoice_amount(item))
        # add_invoice 内部已经 `_sync_invoice_flag` 把发票状态同步成「已收到」，不用再写一遍。
        #
        # **「还没挂过」这个判断必须在事务里做**：上面的前置检查只是快速失败，
        # 挡不住并发 —— 后台自动挂载和人工点「挂上」同时进来，两边都通过前置检查，
        # 就会给同一个文件插两条 invoices 记录（合并打印重复、报销重复，且事后难查）。
        # 用 `WHERE status='pending'` 做条件更新：只有一方抢得到，另一方 rowcount=0。
        cur = c.execute(
            'UPDATE mail_inbox SET status = ?, item_id = ?, invoice_id = ?, auto_attached = ? '
            'WHERE id = ? AND status = ?',
            (repo.MAIL_ATTACHED, item_id, inv_id, 1 if auto else 0,
             mail_id, repo.MAIL_PENDING))
        if cur.rowcount == 0:
            # 抛出去让整个事务回滚：刚插的 invoice 记录跟着撤销，不留孤儿
            raise ValueError('这张发票已经挂过了（可能别人刚挂上），请刷新后再看')
    # 待归档那份文件在**提交之后**才删：事务回滚了文件不能先没了
    invoicing.delete_file(row['rel_path'])
    return {
        'invoice_id': inv_id,
        'stored_name': rec['stored_name'],
        'item': repo.get_item(item_id),
        'invoice': repo.get_invoice(inv_id),
        'duplicate_of_existing': rec['state'] == 'duplicate',
    }


MAX_COMBO_ITEMS = 3        # 一张票最多认定能盖几条采购
COMBO_TOLERANCE = 0.02     # 金额容差（元）：票面与条目合计允许差 2 分


def attach_to_items(mail_id, item_ids, auto=False) -> dict:
    """把待归档的发票挂到**多条**采购上（一票多条）。

    淘宝一个订单买几样、商家只开一张票 —— 这是常态。
    发票归档到**第一条**所属轮次的目录，其余各条通过关联表挂上同一张票：
    合并打印时一张票只印一次，但每一条的背面信息都能带出来。

    要求所有条目在**同一个轮次**：跨轮次既没法归档到单一目录，
    也说不清这张票算哪个月的账。
    """
    ids = [int(x) for x in (item_ids or [])]
    if not ids:
        raise ValueError('至少要挂到一条采购上')
    if len(ids) == 1:
        return attach_to_item(mail_id, ids[0], auto=auto)

    row = repo.get_mail_attachment(mail_id)
    if not row:
        raise ValueError('这条待归档记录不存在')
    if row['status'] == repo.MAIL_ATTACHED:
        raise ValueError('这张发票已经挂到条目上了')

    items = [repo.get_item(i) for i in ids]
    if any(it is None for it in items):
        raise ValueError('要挂到的条目不存在')
    if len({it['round_id'] for it in items}) != 1:
        raise ValueError('一张票只能挂到同一个轮次里的条目上')

    main = items[0]
    rnd = repo.get_round(main['round_id'])
    if not rnd:
        raise ValueError('条目所属的轮次不存在')

    src = Path(config.DATA_DIR) / row['rel_path']
    if not src.exists():
        # 同上：并发时先说清「已经被挂走了」，别让人以为文件坏了。
        again = repo.get_mail_attachment(mail_id)
        if again and again['status'] == repo.MAIL_ATTACHED:
            raise ValueError('这张发票已经挂到条目上了（可能是别人刚挂的）')
        raise ValueError('待归档的发票文件找不到了：%s' % row['rel_path'])
    data = src.read_bytes()

    rec = invoicing.store_invoice(main, rnd['name'],
                                  row['orig_name'] or row['stored_name'], data)
    # 票面金额才是这张票的「价税合计」。一票多条时用主条目的钱会印错 ——
    # 背面印的是**这张票的总额**，不是其中某一条的。
    face = _invoice_face_amount(row)
    if face is None:
        # 读不出票面（照片/扫描件）就按覆盖的几条合计估
        face = rules.r2(sum((rules.to_num(it.get('total')) or 0.0) for it in items))
    # 一票多条也是**一个事务**：登记 + 关联 + 改待归档状态要么全成、要么全不成
    with db_session() as c:
        inv_id = repo.add_invoice(main['id'], main['round_id'], rec['orig_name'],
                                  rec['stored_name'], rec['rel_path'], rec['size'],
                                  conn=c, amount=face)
        # 其余各条挂同一张票；link_invoice 会顺带把它们的发票状态同步成「已收到」
        for it in items[1:]:
            repo.link_invoice(inv_id, it['id'], conn=c)
        # 和单条那条一样：**「还没挂过」必须在事务里判**，否则并发会重复登记。
        cur = c.execute(
            'UPDATE mail_inbox SET status = ?, item_id = ?, invoice_id = ?, auto_attached = ? '
            'WHERE id = ? AND status = ?',
            (repo.MAIL_ATTACHED, main['id'], inv_id, 1 if auto else 0,
             mail_id, repo.MAIL_PENDING))
        if cur.rowcount == 0:
            raise ValueError('这张发票已经挂过了（可能别人刚挂上），请刷新后再看')
    invoicing.delete_file(row['rel_path'])
    return {
        'invoice_id': inv_id,
        'stored_name': rec['stored_name'],
        'item': repo.get_item(main['id']),
        'items': [repo.get_item(it['id']) for it in items],
        'invoice': repo.get_invoice(inv_id),
        'duplicate_of_existing': rec['state'] == 'duplicate',
    }


def sum_combinations(amount, items, min_items=2, max_items=MAX_COMBO_ITEMS,
                     tol=COMBO_TOLERANCE, stop_after=None):
    """找出「几笔采购金额加起来正好等于这张票」的组合（只在同一轮次内拼）。

    为什么需要：淘宝一个订单买几样、商家只开一张票 —— 票面金额是几条之和，
    单条永远对不上，这种票以前只能人工挂。

    **按组合大小从小到大找**（默认 2 条起）：调用方应当先试 2 条，
    2 条唯一就可以定了 —— 不要再去试 3 条。实测条目里一堆十几二十块的小额，
    3 条凑数几乎必然出现大量巧合（¥192 用 2 条只有 1 种拼法，用 3 条有 42 种），
    往大找只会把本来确定的答案冲掉。

    **性能**：先按金额排序，再用「部分和超了立刻 break」剪枝 ——
    朴素枚举 C(n,3) 在缺票条目到 500 条时要 2000 万次组合（单张票 15~25 秒，
    自动收信直接不可用）。调用方一般只关心「0 种 / 1 种 / 多种」，传
    `stop_after=2` 找到两种就能提前返回。

    返回 `[[item, ...], ...]`。**调用方必须要求恰好一种拼法**才敢动手。
    """
    if amount is None:
        return []
    eps = tol + 1e-9
    by_round = {}
    for it in items:
        by_round.setdefault(it.get('round_id'), []).append(it)

    out = []
    for group in by_round.values():
        vals = []
        for it in group:
            v = it.get('paid')
            if v is None:
                v = it.get('total')
            if v is not None:
                vals.append((it, float(v)))
        if len(vals) < max(2, min_items):
            continue
        # 排序是剪枝的前提：一旦部分和超了目标，后面更大的都不用看了
        vals.sort(key=lambda pair: pair[1])
        n = len(vals)

        for k in range(min_items, max_items + 1):
            if n < k:
                break
            # 能凑出来的必要条件：最小的 k 个不超过目标、最大的 k 个不少于目标。
            # 票面金额远超/远小于这一组的量级时，这里就直接跳过了。
            if sum(v for _, v in vals[:k]) > amount + eps:
                continue
            if sum(v for _, v in vals[-k:]) < amount - eps:
                continue

            for idx in _k_sums(vals, k, amount, eps):
                out.append([vals[i][0] for i in idx])
                if stop_after and len(out) >= stop_after:
                    return out
    return out


def _k_sums(vals, k, amount, eps):
    """在已排序的 `vals` 里找恰好 k 个下标，使其金额之和 ≈ amount。

    朴素组合在 k=3、n=500 时是 2000 万次；排序 + 部分和超界即 break 之后，
    实际只扫到「刚好超过目标」的位置就停 —— 金额量级越分散剪得越狠。
    """
    n = len(vals)
    if k == 1:
        for i in range(n):
            if abs(vals[i][1] - amount) <= eps:
                yield (i,)
        return
    if k == 2:
        for i in range(n - 1):
            if vals[i][1] > amount + eps:
                break
            # 下界：连**最大**的那个加起来都不够 → 换下一个 i（i 更大，只会更不够）
            if vals[i][1] + vals[n - 1][1] < amount - eps:
                continue
            for j in range(i + 1, n):
                s = vals[i][1] + vals[j][1]
                if s > amount + eps:
                    break
                if s >= amount - eps:
                    yield (i, j)
        return
    if k == 3:
        for a in range(n - 2):
            if vals[a][1] > amount + eps:
                break
            # 下界：a + 最大的两个都不够 → **换下一个 a**（a 越大和越大，后面可能够）。
            # 注意这里是 continue 不是 break —— 写成 break 会漏掉后面的组合。
            if vals[a][1] + vals[n - 2][1] + vals[n - 1][1] < amount - eps:
                continue
            for b in range(a + 1, n - 1):
                ab = vals[a][1] + vals[b][1]
                if ab > amount + eps:
                    break
                # 下界：b 之后全是更小的数，加上最大的两个也不够 → 换下一个 b。
                # 没有这一句时，「票面远大于池里大部分金额」的场景会退化成
                # 完整的 C(n,3) —— 500 条要跑 2000 万次（单张票十几秒）。
                if ab + vals[n - 2][1] + vals[n - 1][1] < amount - eps:
                    continue
                for c in range(b + 1, n):
                    s = ab + vals[c][1]
                    if s > amount + eps:
                        break
                    if s >= amount - eps:
                        yield (a, b, c)
        return
    # 更大的 k 不枚举（MAX_COMBO_ITEMS 就是 3，走不到这里）


def ignore(mail_id) -> bool:
    """这张不是我要的发票 —— 从待归档区撤下（连文件一起删）。"""
    row = repo.get_mail_attachment(mail_id)
    if not row:
        return False
    return repo.delete_mail_attachment(mail_id)
