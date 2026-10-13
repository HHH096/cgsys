# -*- coding: utf-8 -*-
"""邮箱自动收发票（IMAP）。

只做两件事：**把附件收下来**、按内容去重；**不自动猜**它属于哪一笔采购
（猜错比手工点一下更麻烦）。收下来的进「待归档」列表，由人一键挂到条目。

IMAP 交互移植自现有发票助手的 `app/core/mailbox.py`，保留了几个坑的处理：

  * QQ / 163 要求客户端先发一个 ID 命令，否则可能报 «Unsafe Login»
  * 邮件头是 RFC2047 编码的，而且经常是 gb18030
  * imaplib 抛出的异常参数是 bytes，直接 str() 会得到 b'...'
  * IMAP 的 SINCE 日期必须是英文月份，而 `strftime('%b')` 在中文 Windows 上会给出中文月份

网络交互全部走 `ImapClient` 这层薄封装，测试可以注入替身，不碰真实邮箱。
"""
import base64
import hashlib
import imaplib
import json
import os
import re
import ssl
from dataclasses import asdict, dataclass, field, fields
from datetime import date, timedelta
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from app import config

INVOICE_EXT = {'.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp', '.xml', '.ofd'}
DEFAULT_KEYWORDS = ['发票', 'invoice', 'fapiao', '电子发票', '增值税']

# (显示名, 服务器, 端口)
PRESETS = [
    ('QQ邮箱', 'imap.qq.com', 993),
    ('腾讯企业邮箱', 'imap.exmail.qq.com', 993),
    ('163邮箱', 'imap.163.com', 993),
    ('126邮箱', 'imap.126.com', 993),
    ('阿里云邮箱', 'imap.aliyun.com', 993),
    ('新浪邮箱', 'imap.sina.com', 993),
    ('Gmail', 'imap.gmail.com', 993),
    ('Outlook / Office365', 'outlook.office365.com', 993),
    ('自定义', '', 993),
]

OBFUSCATE_PREFIX = 'cgsys:'
_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


# ------------------------------------------------------------------ 配置

@dataclass
class MailConfig:
    """邮箱收信配置。"""
    host: str = ''
    port: int = 993
    user: str = ''
    password: str = ''            # 授权码（QQ/163 不是登录密码）
    folder: str = 'INBOX'
    use_ssl: bool = True
    days: int = 30                # 只收最近 N 天
    unseen_only: bool = False     # 只收未读
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    any_attachment: bool = False  # 收下所有附件（不看关键字）
    mark_read: bool = False
    max_mails: int = 200          # 一次最多扫多少封
    auto_interval: int = 0        # 定时收取间隔（分钟），0 = 关闭

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['password'] = obfuscate(self.password)
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> 'MailConfig':
        data = dict(data or {})
        data['password'] = deobfuscate(data.get('password', ''))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def ready(self) -> bool:
        return bool(self.host and self.user)


def obfuscate(text: str) -> str:
    """授权码做一层混淆（别让它在配置文件里明文躺着）。本机文件，请勿外传。"""
    if not text:
        return ''
    raw = (OBFUSCATE_PREFIX + text).encode('utf-8')
    return base64.b64encode(bytes(b ^ 0x5A for b in raw)).decode('ascii')


def deobfuscate(text: str) -> str:
    """解混淆。认不出是混淆格式（例如手工填的明文）就原样返回。"""
    if not text:
        return ''
    try:
        raw = bytes(b ^ 0x5A for b in base64.b64decode(text.encode('ascii')))
        s = raw.decode('utf-8')
        return s[len(OBFUSCATE_PREFIX):] if s.startswith(OBFUSCATE_PREFIX) else s
    except Exception:
        return text


def load_config(path=None) -> MailConfig:
    p = Path(path or config.MAIL_CONFIG)
    if not p.exists():
        return MailConfig()
    try:
        return MailConfig.from_dict(json.loads(p.read_text(encoding='utf-8')))
    except Exception:
        return MailConfig()


def save_config(cfg: MailConfig, path=None) -> Path:
    p = Path(path or config.MAIL_CONFIG)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    return p


# ------------------------------------------------------------------ 小工具

def imap_date(d: date) -> str:
    """IMAP 的日期格式，必须是英文月份 —— 不能用 strftime('%b')，
    它在中文 Windows 上会给出「1月」这种，服务器不认识。"""
    return '%02d-%s-%d' % (d.day, _MONTHS[d.month - 1], d.year)


def build_criteria(cfg: MailConfig, today: Optional[date] = None) -> str:
    """拼 IMAP 搜索条件。"""
    parts = []
    days = int(cfg.days or 0)
    if days > 0:
        parts.append('SINCE %s' % imap_date((today or date.today()) - timedelta(days=days)))
    if cfg.unseen_only:
        parts.append('UNSEEN')
    return ' '.join(parts) if parts else 'ALL'


def decode_mime_header(value) -> str:
    """RFC2047 邮件头解码；编码名不可信，逐级回退。"""
    if not value:
        return ''
    out = []
    try:
        parts = decode_header(value)
    except Exception:
        return str(value)
    for text, enc in parts:
        if isinstance(text, bytes):
            for candidate in (enc, 'utf-8', 'gb18030', 'latin-1'):
                if not candidate:
                    continue
                try:
                    out.append(text.decode(candidate))
                    break
                except Exception:
                    continue
            else:
                out.append(text.decode('utf-8', 'ignore'))
        else:
            out.append(text)
    return ''.join(out)


def safe_filename(name: str, fallback: str = 'attachment') -> str:
    name = os.path.basename(decode_mime_header(name) or fallback)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name).strip() or fallback
    return name[:120]


def looks_like_invoice(text: str, keywords: Sequence[str], any_attachment: bool) -> bool:
    if any_attachment:
        return True
    low = (text or '').lower()
    return any(k.lower() in low for k in keywords if k)


def error_text(exc) -> str:
    """imaplib 的异常参数是 bytes，直接 str() 会得到 b'...'，这里翻译一下。"""
    args = getattr(exc, 'args', None) or [exc]
    msg = args[0]
    if isinstance(msg, bytes):
        for enc in ('utf-8', 'gb18030', 'latin-1'):
            try:
                return msg.decode(enc)
            except Exception:
                continue
        return repr(msg)
    return str(msg)


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def ext_of_content_type(ctype: str) -> str:
    return {'application/pdf': '.pdf', 'image/png': '.png', 'image/jpeg': '.jpg',
            'image/tiff': '.tif', 'image/bmp': '.bmp', 'image/webp': '.webp',
            'text/xml': '.xml', 'application/xml': '.xml'}.get(ctype, '.bin')


# ------------------------------------------------------------------ 附件解析（不联网）

def iter_attachments(msg) -> List:
    """从一封邮件里取出所有附件：[(文件名, bytes)]。"""
    out: List = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get('Content-Disposition') or '')
            filename = part.get_filename()
            if not filename and 'attachment' not in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            ctype = (part.get_content_type() or '').lower()
            name = safe_filename(filename or ('attachment' + ext_of_content_type(ctype)))
            out.append((name, payload))
    else:
        filename = msg.get_filename()
        payload = msg.get_payload(decode=True)
        if filename and payload:
            out.append((safe_filename(filename), payload))
    return out


def filter_attachments(msg, cfg: MailConfig) -> List:
    """挑出「看起来是发票」的附件。"""
    subject = decode_mime_header(msg.get('Subject', ''))
    sender = decode_mime_header(msg.get('From', ''))
    out: List = []
    for name, payload in iter_attachments(msg):
        if os.path.splitext(name)[1].lower() not in INVOICE_EXT:
            continue
        haystack = '%s %s %s' % (subject, name, sender)
        if not looks_like_invoice(haystack, cfg.keywords or DEFAULT_KEYWORDS, cfg.any_attachment):
            continue
        out.append((name, payload))
    return out


# ------------------------------------------------------------------ IMAP 交互

class ImapClient:
    """imaplib 的薄封装。测试可以传一个实现了同样方法的替身进来。"""

    def __init__(self, conn):
        self.conn = conn

    def select(self, folder):
        return self.conn.select(folder, readonly=True)

    def search_uids(self, criteria: str) -> List[str]:
        typ, data = self.conn.uid('search', None, criteria)
        if typ != 'OK':
            return []
        blob = (data[0] if data else b'') or b''
        return [b.decode('ascii', 'ignore') for b in blob.split()]

    def fetch_raw(self, uid: str) -> bytes:
        typ, data = self.conn.uid('fetch', uid, '(RFC822)')
        if typ != 'OK':
            raise RuntimeError('取信失败：%s' % uid)
        for part in data or []:
            if isinstance(part, tuple) and len(part) > 1 and part[1]:
                return part[1]
        return b''

    def logout(self):
        try:
            self.conn.logout()
        except Exception:
            pass


# 163 / QQ 等要求客户端先自报身份（IMAP 的 ID 命令）。
# ⚠️ imaplib **默认没有注册 ID**，不注册的话 _simple_command('ID', ...) 会抛
# 「unknown command」，而且会被下面的 except 静默吞掉 —— 表现成
# 「连得上、但读不了邮件」（163 会回 Unsafe Login）。
imaplib.Commands['ID'] = ('AUTH', 'SELECTED')


def send_client_id(conn) -> None:
    """自报身份，免得 QQ / 163 报 «Unsafe Login»。

    ⚠️ **必须在 login 之后调用**。163 对「登录前就报身份」的客户端会判成
    不安全客户端，回 `EXAMINE Unsafe Login. Please contact kefu@188.com`，
    症状是能连上、却读不到邮件。
    """
    try:
        conn._simple_command(
            'ID', '("name" "procurement" "version" "1.0" "vendor" "cgsys")')
        conn._untagged_response('OK', [None], 'ID')
    except Exception:
        pass


def open_connection(cfg: MailConfig, timeout: int = 30):
    """建立并登录真实 IMAP 连接（失败抛异常，调用方负责提示）。

    ⚠️ **不要给 IMAP4 / IMAP4_SSL 传 `timeout`** —— 那个参数是 Python **3.9**
    才加的。阿里云 ECS 自带的是 **3.8**，传进去会报
    `__init__() got an unexpected keyword argument 'timeout'`，
    而我的开发机是 3.9，**所以这个 bug 在本地测不出来，只有真机上才暴露**。
    """
    if not cfg.host:
        raise ValueError('请先填写邮箱服务器（如 imap.qq.com）')
    if cfg.use_ssl:
        conn = imaplib.IMAP4_SSL(cfg.host, cfg.port,
                                 ssl_context=ssl.create_default_context())
    else:
        conn = imaplib.IMAP4(cfg.host, cfg.port)
        try:
            conn.starttls(ssl_context=ssl.create_default_context())
        except Exception:
            pass
    conn.login(cfg.user, cfg.password)
    send_client_id(conn)          # ← 登录之后再报身份（163 要求这个顺序）
    return conn


def send_client_id(conn) -> None:
    """QQ / 163 等要求先自报身份，否则可能报 «Unsafe Login»。失败无所谓。"""
    try:
        conn._simple_command('ID', '("name" "procurement" "version" "1.0" "vendor" "local")')
        conn._untagged_response('OK', [None], 'ID')
    except Exception:
        pass


def test_connection(cfg: MailConfig) -> Dict:
    """试连一下，返回 {'ok': bool, 'message': str}。"""
    if not cfg.ready:
        return {'ok': False, 'message': '请先填写邮箱服务器和账号'}
    try:
        conn = open_connection(cfg)
        try:
            typ, data = conn.select(cfg.folder or 'INBOX', readonly=True)
            total = (data[0] or b'0').decode('ascii', 'ignore') if data and data[0] else '0'
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return {'ok': True,
                'message': '连接成功：%s 的 %s 共 %s 封邮件' % (cfg.host, cfg.folder or 'INBOX', total)}
    except imaplib.IMAP4.error as exc:
        return {'ok': False,
                'message': '登录失败：%s（QQ/163 等要填「授权码」，不是登录密码）' % error_text(exc)}
    except Exception as exc:
        return {'ok': False, 'message': '连接失败：%s' % error_text(exc)}


# ------------------------------------------------------------------ 收信

def fetch_attachments(cfg: MailConfig, client=None, skip_uids: Sequence[str] = (),
                      on_log: Optional[Callable[[str], None]] = None) -> Dict:
    """收一次信。

    client 为 None 时自动连真实邮箱；测试可注入替身。
    去重交给调用方：传 skip_uids（已经从库里收过的那批 UID）进来即可。
    """
    log = on_log or (lambda _m: None)
    result = {'ok': True, 'message': '', 'scanned': 0,
              'attachments': [], 'skipped': [], 'errors': []}

    if not cfg.host:
        result.update(ok=False, message='请先填写邮箱服务器（如 imap.qq.com）')
        return result
    if not cfg.user:
        result.update(ok=False, message='请先填写邮箱账号')
        return result

    owns = client is None
    skip = {str(u) for u in (skip_uids or [])}
    try:
        if owns:
            client = ImapClient(open_connection(cfg))
        client.select(cfg.folder or 'INBOX')
        criteria = build_criteria(cfg)
        log('搜索条件：%s' % criteria)

        uids = client.search_uids(criteria)
        limit = max(1, int(cfg.max_mails or 200))
        if len(uids) > limit:
            log('命中 %d 封，只处理最近 %d 封' % (len(uids), limit))
            uids = uids[-limit:]
        result['scanned'] = len(uids)

        for uid in uids:
            if uid in skip:
                result['skipped'].append({'uid': uid, 'reason': '这封之前已经收过'})
                continue
            try:
                raw = client.fetch_raw(uid)
            except Exception as exc:
                result['errors'].append('取第 %s 封失败：%s' % (uid, error_text(exc)))
                continue
            if not raw:
                result['errors'].append('第 %s 封取回来是空的' % uid)
                continue

            msg = message_from_bytes(raw)
            subject = decode_mime_header(msg.get('Subject', ''))
            sender = decode_mime_header(msg.get('From', ''))
            when = decode_mime_header(msg.get('Date', ''))
            picked = filter_attachments(msg, cfg)
            if not picked:
                result['skipped'].append({'uid': uid, 'reason': '没有像发票的附件'})
                continue
            for name, data in picked:
                result['attachments'].append({
                    'uid': uid, 'subject': subject, 'sender': sender, 'date': when,
                    'filename': name, 'data': data,
                    'sha1': sha1_of(data), 'size': len(data),
                })
            log('第 %s 封「%s」→ %d 个附件' % (uid, subject or '(无主题)', len(picked)))
    except Exception as exc:
        result.update(ok=False, message='收取失败：%s' % error_text(exc))
    finally:
        if owns and client is not None:
            client.logout()
    return result
