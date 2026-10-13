# -*- coding: utf-8 -*-
"""组员名单预置：读根目录 `组员名单.txt`，把还没有的人补进库。

文件格式（每行一个人）：

    姓名,手机号
    *张三,13800000000      ← 行首 * 表示负责人

以 `#` 开头的行是注释、空行忽略；手机号可以留空；
分隔符逗号、中文逗号、制表符、空格都认。

重复导入是安全的：已有的人不会被改名或清空手机号，
只在「原来没填手机号」时补上手机号。

⚠️ **不动 `is_owner`**：名单里那个 `*` 只决定「**第一次**把这个人加进来时算不算负责人」。
已经存在的人，负责人标记**只认界面上改的**。
（以前这里会「补负责人标记」，于是每重启一次服务，用户在「组员管理」里取消掉的
负责人就被重新设回来 —— 线上真发生过，用户报「老是取消不了」。）
"""
import re
from pathlib import Path

SEPARATORS = re.compile(r'[,，\t ]+')
PHONE_OK = re.compile(r'[0-9+\-]{5,20}\Z')
OWNER_MARKS = ('负责人', 'owner', 'OWNER', '*', '*owner')
# 忘了加分隔符：姓名紧跟手机号（从别处复制名单时很常见）。
#     王五13900000000  ->  姓名「王五」+ 手机号 13900000000
# 要求姓名以非数字结尾，免得把一串数字当成名字。
NAME_THEN_PHONE = re.compile(r'^(?P<name>.*?\D)(?P<phone>1[3-9]\d{9})$')


def parse_roster(text):
    """解析名单文本 -> ([(姓名, 手机号, 是否负责人)], [坏行])。"""
    people, bad = [], []
    for lineno, raw in enumerate((text or '').splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue

        is_owner = False
        if line[0] in '*＊':
            is_owner = True
            line = line[1:].strip()

        parts = [p for p in SEPARATORS.split(line) if p]
        if not parts:
            continue

        name = parts[0].strip()
        phone = parts[1].strip() if len(parts) > 1 else ''
        if len(parts) > 2 and parts[2] in OWNER_MARKS:
            is_owner = True

        # 少写了分隔符时也能认出来：姓名紧跟着手机号
        if not phone:
            m = NAME_THEN_PHONE.match(name)
            if m:
                name, phone = m.group('name').strip(), m.group('phone')

        if not name:
            bad.append({'line': lineno, 'text': raw, 'why': '没写姓名'})
            continue
        if phone and not PHONE_OK.match(phone):
            bad.append({'line': lineno, 'text': raw, 'why': '手机号格式不对'})
            continue
        people.append((name, phone, is_owner))
    return people, bad


def read_roster(path=None):
    """读名单文件；文件不存在时返回 None（不报错）。"""
    from app import config
    p = Path(path or config.ROSTER_FILE)
    if not p.exists():
        return None, p
    return p.read_text(encoding='utf-8-sig', errors='replace'), p


def import_roster(path=None):
    """把名单里还没进库的人补进库。返回一份可打印的摘要。"""
    from app.core import repo

    text, p = read_roster(path)
    summary = {'file': str(p), 'exists': text is not None,
               'added': [], 'updated': [], 'bad': []}
    if text is None:
        return summary

    people, bad = parse_roster(text)
    summary['bad'] = bad

    known = {m['name']: m for m in repo.list_members()}
    seen = set()
    for name, phone, is_owner in people:
        if name in seen:
            summary['bad'].append({'line': 0, 'text': name, 'why': '名单里重复出现'})
            continue
        seen.add(name)

        cur = known.get(name)
        if cur is None:
            try:
                repo.add_member(name, phone, 1 if is_owner else 0)
            except Exception:
                summary['bad'].append({'line': 0, 'text': name, 'why': '写库失败'})
                continue
            summary['added'].append(name)
            known[name] = {'id': None, 'name': name, 'phone': phone, 'is_owner': int(is_owner)}
            continue

        # 已有的人：**只补空着的手机号**。
        #
        # ⚠️ 特意不动 is_owner：那是人在「组员管理」里勾出来的决定，
        #    名单里那个 `*` 只管"第一次把人加进来"。
        #    以前这里有一句「补负责人标记」，于是每重启一次服务就把用户
        #    取消掉的负责人又设回来 —— 线上真发生过（用户报"老是取消不了"）。
        patch = {}
        if phone and not (cur.get('phone') or '').strip():
            patch['phone'] = phone
        if patch:
            repo.update_member(cur['id'], **patch)
            summary['updated'].append(name)
    return summary
