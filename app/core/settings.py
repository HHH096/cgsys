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
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from app import config
from app.core.mailbox import deobfuscate, obfuscate

SETTINGS_PATH = 'settings.json'      # 放在 data 下（**运行时**拼路径，别在模块级定常量）
SETUP_CODE_PATH = 'setup-code.txt'   # 首次设密码用的初始化码（设完就删）
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


# ------------------------------------------------- 首次设密码的初始化码

def setup_code_file() -> Path:
    return Path(config.DATA_DIR) / SETUP_CODE_PATH


def setup_code() -> str:
    """首次设密码需要的初始化码（没有就现生成一个）。

    为什么需要它：还没设过密码时，`POST /api/purchase/password` 必须能**匿名**调用
    （不然就是死锁：进不去→设不了密码）。可服务一旦暴露到公网，
    任何知道地址的人都能抢先把密码设了、把采购权限拿走。

    所以第一次设密码要带这个码 —— 它只写在**服务器磁盘**（`data/setup-code.txt`）
    和启动日志里，外面的人拿不到。设好密码后这个文件会被删掉。
    """
    p = setup_code_file()
    try:
        if p.exists():
            code = p.read_text(encoding='utf-8').strip()
            if code:
                return code
    except OSError:
        pass
    config.ensure_dirs()
    code = secrets.token_hex(4).upper()          # 8 位十六进制，好抄
    try:
        p.write_text(code + '\n', encoding='utf-8')
    except OSError:
        pass
    return code


def verify_setup_code(code: str) -> bool:
    """初始化码对不对（定长比较、忽略大小写和首尾空格）。

    **只读不生成**：拿不到码文件就是「不对」。如果这里顺手生成一个新码，
    那校验永远对不上（刚生成的又和用户手里的不是同一个）。
    """
    if not (code or '').strip():
        return False
    try:
        stored = setup_code_file().read_text(encoding='utf-8').strip()
    except OSError:
        return False
    if not stored:
        return False
    # 先编码成 bytes：compare_digest 对 str 只吃 ASCII，
    # 用户手滑打中文进来会直接抛 TypeError（500）—— 这坑在 check_password 里踩过。
    return secrets.compare_digest((code or '').strip().upper().encode('utf-8'),
                                  stored.upper().encode('utf-8'))


def clear_setup_code() -> None:
    """密码设好了就把它删掉，别一直躺在磁盘上。"""
    try:
        setup_code_file().unlink()
    except OSError:
        pass


# ------------------------------------------------- 组员身份的签名

MEMBER_SECRET_KEY = 'member_secret'


def member_secret() -> str:
    """签组员 cookie 的密钥（首次用时生成，落在 settings.json 里）。

    **为什么要签**：组员身份原本就是 cookie 里明文的 member id ——
    谁把 `cgsys_member=7` 一改就成了别人，改条目、删条目、改实付、删发票全能做，
    `_guard_own_item` 那一层形同虚设。

    密钥落盘（不是放内存），所以服务重启不会让所有人重选名字。
    """
    data = _load_raw()
    s = data.get(MEMBER_SECRET_KEY)
    if isinstance(s, str) and s:
        return s
    s = secrets.token_urlsafe(32)
    data[MEMBER_SECRET_KEY] = s
    _save_raw(data)
    return s


def _member_sig(raw: str) -> str:
    return hmac.new(member_secret().encode('utf-8'),
                    raw.encode('utf-8'), hashlib.sha256).hexdigest()[:32]


def sign_member(mid) -> str:
    """把组员 id 签成 cookie 值：`7.<签名>`。"""
    raw = str(int(mid))
    return '%s.%s' % (raw, _member_sig(raw))


def read_member(value) -> Optional[int]:
    """验签并取出组员 id。

    对不上就返回 None —— 老格式（纯数字）、被人改过的、乱填的，一律当「没选名字」。
    也就是说升级后**每个人都要重选一次名字**，这是故意的：旧 cookie 本身不可信。
    """
    if not value or '.' not in str(value):
        return None
    raw, sig = str(value).rsplit('.', 1)
    if not secrets.compare_digest(sig, _member_sig(raw)):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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


def revoke_kind(kind: str) -> int:
    """吊销某一类身份的所有登录（返回吊销了几个）。

    为什么需要：把组员口令关掉（或改掉）之后，老组员手里的 cookie 还能再用 30 天 ——
    等于「关了门但钥匙还能开」。采购自己的登录不能跟着失效，所以按身份分类清。
    """
    n = 0
    for token, (_, k) in list(_tokens.items()):
        if k == kind:
            _tokens.pop(token, None)
            n += 1
    return n
