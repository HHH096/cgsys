# -*- coding: utf-8 -*-
"""采购入口的密码与登录状态。

* **密码**做混淆后存在 `data/settings.json`（和邮箱配置分开文件，别混在一起）
* **登录状态**放内存：登录成功发一个随机 token 写进 cookie，服务一重启就全部失效
  —— 局域网内部工具，这比持久化省心，也更安全

关于组员入口：**它按设计就没有密码**。组员身份靠「选名字」后写下的 cookie 传递，
属于**「君子协定」级别**：防手滑改错别人的东西，**不防存心使坏**。
真正的硬边界只有一条，并且是在服务端强制的：

    「采购人员专用接口必须已登录」

组员能做什么、不能做什么，最终都归到这条上。
"""
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from app import config
from app.core.mailbox import deobfuscate, obfuscate

SETTINGS_PATH = 'settings.json'      # 放在 data 下（**运行时**拼路径，别在模块级定常量）
COOKIE_NAME = 'cgsys_purchase'       # 采购人员的登录凭证
MEMBER_COOKIE = 'cgsys_member'       # 组员选的名字（弱身份，仅用于归属判断）
MEMBER_PASS = 'cgsys_member_pass'    # 组员入口的通行证（全组共用一个口令换来的）

# 两种身份：采购人员（能进 /purchase），组员（能填单看条目）
KIND_PURCHASE = 'purchase'
KIND_MEMBER = 'member'

MAX_FAILS = 8                       # 同一 IP 连续失败这么多次就锁一会儿
LOCK_SECONDS = 300
TOKEN_TTL = 12 * 3600               # 采购登录管 12 小时
MEMBER_TTL = 30 * 24 * 3600         # 组员的口令输一次管 30 天（他们不该天天输）

_tokens: Dict[str, tuple] = {}      # token -> (过期时间戳, 身份)
_fails: Dict[str, dict] = {}        # ip -> {'n': 次数, 'until': 锁定到}


# ------------------------------------------------------------------ 读写

def settings_file() -> Path:
    """**每次现算** —— 不能在模块级写成常量：那样测试里 patch 了 DATA_DIR 也改不动它，
    配置文件就会落到真实目录里去（这个坑在 support.py 里已经记过一次）。"""
    return Path(config.DATA_DIR) / SETTINGS_PATH


def _load_raw() -> dict:
    p = settings_file()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(data: dict) -> None:
    config.ensure_dirs()
    settings_file().write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8')


# ------------------------------------------------------------------ 密码

def has_password() -> bool:
    """设过没有。没设过时前端要引导先设一个，而不是留空放行。"""
    return bool(_load_raw().get('purchase_password'))


def set_password(new: str) -> None:
    data = _load_raw()
    data['purchase_password'] = obfuscate(new) if new else ''
    _save_raw(data)


def check_password(pw: str) -> bool:
    stored = _load_raw().get('purchase_password') or ''
    if not stored or not pw:
        return False
    # 定长比较，别让耗时泄露信息。
    # 注意 compare_digest 对 str 只吃 ASCII，所以先编码成 bytes —— 中文密码也能用。
    return secrets.compare_digest(deobfuscate(stored).encode('utf-8'),
                                  (pw or '').encode('utf-8'))


# ------------------------------------------------- 组员入口的共享口令

def has_member_password() -> bool:
    """给组员入口设过口令没有。

    **没设过就等于组员入口不可用**（而不是放行）——
    一旦服务通过隧道暴露到公网，留一道敞开的门比不方便严重得多。
    """
    return bool(_load_raw().get('member_password'))


def set_member_password(pw: str) -> None:
    data = _load_raw()
    data['member_password'] = obfuscate(pw) if pw else ''
    _save_raw(data)


def check_member_password(pw: str) -> bool:
    stored = _load_raw().get('member_password') or ''
    if not stored or not pw:
        return False
    return secrets.compare_digest(deobfuscate(stored).encode('utf-8'),
                                  (pw or '').encode('utf-8'))


# ------------------------------------------------------------------ 失败限速

def _now() -> float:
    return time.time()


def locked_for(ip: str) -> int:
    """这个 IP 还要锁几秒（0 = 没锁）。"""
    rec = _fails.get(ip or '')
    if not rec:
        return 0
    left = int(rec.get('until', 0) - _now())
    return left if left > 0 else 0


def note_failure(ip: str) -> None:
    rec = _fails.setdefault(ip or '', {'n': 0, 'until': 0.0})
    rec['n'] = rec.get('n', 0) + 1
    if rec['n'] >= MAX_FAILS:
        rec['until'] = _now() + LOCK_SECONDS
        rec['n'] = 0


def clear_failures(ip: str) -> None:
    _fails.pop(ip or '', None)


# ------------------------------------------------------------------ 登录凭证

def issue_token(kind: str = KIND_PURCHASE) -> str:
    ttl = MEMBER_TTL if kind == KIND_MEMBER else TOKEN_TTL
    token = secrets.token_urlsafe(32)
    _tokens[token] = (_now() + ttl, kind)
    return token


def verify_token(token: Optional[str], kind: str = KIND_PURCHASE) -> bool:
    if not token:
        return False
    rec = _tokens.get(token)
    if not rec:
        return False
    exp, got = rec
    if exp < _now():
        _tokens.pop(token, None)
        return False
    return got == kind


def revoke_token(token: Optional[str]) -> None:
    if token:
        _tokens.pop(token, None)


def reset_state() -> None:
    """测试用：清掉登录与限速状态。"""
    _tokens.clear()
    _fails.clear()
