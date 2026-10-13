# -*- coding: utf-8 -*-
"""从「淘宝 / 闲鱼 / 拼多多 / 天猫」分享文案里解析出 名称、链接、价格。

平时拿商品信息的方式是：在 App 里点「分享 → 复制链接」，得到一段
带链接和标题（有时还有价格）的文字，整段粘进来即可。

**不联网。** 实测确认直接抓淘宝链接拿不到商品信息——短链返回的是
唤起 App 的空壳页（只有 `window.location.replace`），既没有商品名也没有价格。
所以这里只做「文案解析」，能认出多少算多少，认不出也不报错。
"""
import re

# 链接：到空白或中文标点为止（淘宝链接里的英文逗号是合法参数分隔，要保留）
URL_RE = re.compile(r'https?://[^\s，。、；）)】」』"\'<>（）【]+')
URL_TAIL_CHARS = ',.;:，。、；：'

# 淘口令：￥AbCdEf￥ / 7₴AbCdEf₴ / ₤xxxx₤
TOKEN_RE = re.compile(r'[¥￥₴₤€]\s*[A-Za-z0-9]{6,}\s*[¥￥₴₤€]'
                      r'|\d?\s*[₴₤€]\s*[A-Za-z0-9]{6,}\s*[₴₤€]')

# 平台标签，不是商品名
PLATFORM_TAGS = ('淘宝', '闲鱼', '拼多多', '天猫', '京东', '抖音', '转转', '唯品会', '小红书')
PLATFORM_RE = re.compile(r'【\s*(?:%s)\s*】' % '|'.join(PLATFORM_TAGS))

# 已知的外层模板：整段话只是为了包住里面的标题
TEMPLATE_RES = [
    re.compile(r'(?:我)?在闲鱼(?:发布了|卖|转卖)?\s*[:：]?\s*[【「『](.+?)[】」』]'),
    re.compile(r'[【「『]\s*转卖\s*[】」』]\s*[【「『](.+?)[】」』]'),
    re.compile(r'分享(?:一件|一个)?(?:好物|宝贝)\s*[:：]?\s*[【「『](.+?)[】」』]'),
]

# 引号对，按优先级排；【】放最后
QUOTE_RES = [
    re.compile(r'「(.+?)」'),
    re.compile(r'『(.+?)』'),
    re.compile(r'“(.+?)”'),
    re.compile(r'"(.+?)"'),
    re.compile(r'【(.+?)】'),
]

# 尾巴上的提示语，不是商品名。按长度降序删，长句优先（否则会被短句拆散、留下「或者」这种残渣）
NOISE_PHRASES = sorted([
    '点击链接直接打开 或者 淘宝搜索直接打开',
    '点击链接直接打开或者淘宝搜索直接打开',
    '复制这条信息，打开手机淘宝',
    '复制这条信息打开手机淘宝',
    '点击链接直接打开', '或者淘宝搜索直接打开', '淘宝搜索直接打开', '打开手机淘宝',
    '复制这条信息', '复制打开淘宝', '打开淘宝APP', '打开淘宝 app', '长按复制',
    '点击链接打开', '直接打开', '打开闲鱼', '打开拼多多', '取口令', '淘口令',
    '点击打开链接', '点击查看详情', '点击链接', '复制口令', '看看这个',
], key=len, reverse=True)

# 上面删完之后可能剩下的孤立连接词，如「排线 或者 」
RESIDUE_RE = re.compile(r'(?<=\s)(?:或者|或)(?=\s|$)')

# 价格：分来源，好让前端填到对的地方（实付 / 售价 / 只是标价）
PRICE_LABEL_RES = [
    (re.compile(r'(?:实付款|实付|成交价|已付)'
                r'\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)'), '实付'),
    (re.compile(r'(?:到手价|券后价|券后)'
                r'\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)'), '到手价'),
    (re.compile(r'(?:价格|售价|单价)'
                r'\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)'), '售价'),
    (re.compile(r'[¥￥]\s*(\d+(?:\.\d+)?)'), '标价'),
    (re.compile(r'(\d+(?:\.\d+)?)\s*元'), '标价'),
]

MAX_NAME_LEN = 80


def _norm(text):
    """全角空格、不间断空格统一，压掉多余空白。"""
    if not text:
        return ''
    s = text.replace('\u3000', ' ').replace('\xa0', ' ').replace('\r\n', '\n')
    s = s.replace('\r', '\n')
    return s


def find_url(text):
    m = URL_RE.search(text or '')
    if not m:
        return ''
    return m.group(0).rstrip(URL_TAIL_CHARS)


def find_price(text):
    """返回 (价格, 来源)。来源为 实付 / 到手价 / 售价 / 标价 之一，认不出则为空串。"""
    for rx, source in PRICE_LABEL_RES:
        m = rx.search(text or '')
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        return value, source
    return None, ''


def _strip_noise(text):
    s = PLATFORM_RE.sub(' ', text)
    s = TOKEN_RE.sub(' ', s)
    s = URL_RE.sub(' ', s)
    for phrase in NOISE_PHRASES:
        s = s.replace(phrase, ' ')
    s = RESIDUE_RE.sub(' ', s)
    return s


def _title_candidates(text):
    """按优先级取出候选标题。"""
    for rx in TEMPLATE_RES:
        got = [m.strip() for m in rx.findall(text) if m and m.strip()]
        if got:
            return got, '模板'

    for rx in QUOTE_RES:
        got = [m.strip() for m in rx.findall(text) if m and m.strip()]
        # 【】里的平台标签不算
        got = [g for g in got if g not in PLATFORM_TAGS]
        if got:
            return got, '引号'

    return [], ''


def clean_name(text):
    """把候选标题收拾成能直接用的物料名称。"""
    s = _strip_noise(text or '')
    s = re.sub(r'[「」『』“”"]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.strip(' -_·,，。、;；:：|')
    return s[:MAX_NAME_LEN]


def parse_share_text(text):
    """解析分享文案。

    返回 dict：name / url / price / price_from / raw / notes（给用户的提示）。
    认不出来不抛异常，只把能认出的部分返回。
    """
    raw = text or ''
    norm = _norm(raw)
    result = {'name': '', 'url': '', 'price': None, 'price_from': '', 'raw': raw, 'notes': []}

    if not norm.strip():
        result['notes'].append('没有内容可解析')
        return result

    result['url'] = find_url(norm)
    price, price_from = find_price(norm)
    result['price'] = price
    result['price_from'] = price_from

    candidates, source = _title_candidates(norm)
    if candidates:
        # 多个候选时取最长的那个；再把噪音清掉
        best = clean_name(max(candidates, key=len))
        # 清完如果变空，退回去用没清过的
        result['name'] = best or max(candidates, key=len).strip()[:MAX_NAME_LEN]
    else:
        # 没有引号包着：把链接、口令、提示语都去掉，取剩下的最长一段
        residue = _strip_noise(norm)
        parts = [p.strip() for p in re.split(r'[\n。；;]', residue) if p.strip()]
        if parts:
            result['name'] = clean_name(max(parts, key=len))

    if not result['url']:
        result['notes'].append('没认出链接，网址可以手动填')
    if not result['name']:
        result['notes'].append('没认出名称，请手动填')
    if result['price'] is None:
        result['notes'].append('文案里没有价格，实付金额请手填')
    if source == '模板':
        result['notes'].append('名称取自闲鱼分享的标题，记得核对一下')
    return result
