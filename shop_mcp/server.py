"""Read-only public shopping discovery, with explicit index/price evidence."""
import asyncio
import os
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
from .evidence import research_product, compare_products, compare_quotes, amount_mentions

from .assistant import Wishlist, check_variants, review_digest, tool_descriptors

from .commerce import PriceStore, calculate_checkout, compare_sellers, commerce_tool_descriptors

ROOT = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser()
HERMES_PYTHON = Path(os.environ.get('SHOPPING_HERMES_PYTHON', str(Path.home() / '.hermes/hermes-agent/venv/bin/python'))).expanduser()
DATA_DIR = Path(os.environ.get('SHOPPING_DATA_DIR', str(HERMES_HOME / 'integrations/shopping-search/data'))).expanduser()
DOMAINS = {'jd': 'item.jd.com', 'taobao': 'item.taobao.com',
           'tmall': 'detail.tmall.com', 'deals': 'smzdm.com'}
LABELS = {'jd': '京东', 'taobao': '淘宝', 'tmall': '天猫', 'deals': '什么值得买'}
NOTE = ('公开网页索引检索，并非平台原生全量搜索。结果可能过时或包含配件；'
        '未验证库存、店铺资质、商品真伪、规格或当前价格。摘要价格不能作为实时到手价。')


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_product_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('INVALID_PRODUCT_URL')
    u = urlsplit(url)
    if u.scheme != 'https' or u.username or u.password or u.port or not u.hostname:
        raise ValueError('REQUIRE_PUBLIC_HTTPS_PRODUCT_URL')
    host = u.hostname.lower()
    if host == 'item.jd.com':
        match = re.fullmatch(r'/([0-9]{5,20})\.html', u.path)
        if match:
            return {'platform': 'jd', 'product_id': match[1],
                    'url': 'https://item.jd.com/' + match[1] + '.html'}
    if host == 'item.m.jd.com':
        match = re.fullmatch(r'/(?:product/)?([0-9]{5,20})\.html', u.path)
        if match:
            return {'platform': 'jd', 'product_id': match[1],
                    'url': 'https://item.jd.com/' + match[1] + '.html'}
    hosts = {'item.taobao.com': 'taobao', 'detail.tmall.com': 'tmall',
             'detail.m.tmall.com': 'tmall', 'h5.m.taobao.com': 'taobao'}
    if host in hosts and u.path in ('/item.htm', '/item_o.htm', '/awp/core/detail.htm'):
        values = parse_qs(u.query).get('id', [])
        if len(values) == 1 and re.fullmatch(r'[A-Za-z0-9_-]{5,128}', values[0]):
            platform = hosts[host]
            canonical_host = 'detail.tmall.com' if platform == 'tmall' else 'item.taobao.com'
            return {'platform': platform, 'product_id': values[0],
                    'url': 'https://' + canonical_host + '/item.htm?' + urlencode({'id': values[0]})}
    raise ValueError('UNSUPPORTED_PRODUCT_URL_USE_FULL_ITEM_LINK')


def relevant(query, title):
    """Reject wrong-model and unrelated index responses; not a SKU equivalence test."""
    title = str(title).casefold()
    if not title or title in ('n/a', 'untitled'):
        return False
    tokens = re.findall(r'[a-zA-Z0-9][a-zA-Z0-9_-]*|[\u4e00-\u9fff]+', query.casefold())
    tokens = [t for t in tokens if len(t) > 1 and t not in ('京东', '淘宝', '天猫', '推荐', '搜索', '价格', '商品')]
    model_tokens = [t for t in tokens if re.search('[a-z]', t) and re.search('[0-9]', t)]
    if model_tokens and not all(re.search(r'(?<![a-z0-9])' + re.escape(t) + r'(?![a-z0-9])', title) for t in model_tokens):
        return False
    return bool(tokens) and any(t in title for t in tokens)


def normalize_rows(raw, query, platform, limit):
    data = raw.get('data') if isinstance(raw, dict) else None
    web = data.get('web') if isinstance(data, dict) else None
    if not isinstance(web, list):
        return [], 0
    rows, discarded, seen = [], 0, set()
    for item in web:
        if not isinstance(item, dict) or not relevant(query, item.get('title', '')):
            discarded += 1
            continue
        try:
            if platform == 'deals':
                u = urlsplit(item.get('url', ''))
                if (u.scheme != 'https' or u.username or u.password or u.port or
                        not (u.hostname == 'smzdm.com' or (u.hostname or '').endswith('.smzdm.com'))):
                    raise ValueError('INVALID_DEAL_URL')
                info = {'platform': 'deals', 'url': 'https://' + u.hostname + u.path}
            else:
                info = parse_product_url(item.get('url'))
                if info['platform'] != platform:
                    raise ValueError('WRONG_PLATFORM')
        except (ValueError, TypeError):
            discarded += 1
            continue
        if info['url'] in seen:
            continue
        seen.add(info['url'])
        rows.append({**info, 'title': str(item['title'])[:350],
                     'snippet': str(item.get('description', ''))[:900],
                     'evidence_kind': 'deal_page_index' if platform == 'deals' else 'product_page_index',
                     'current_price': None, 'current_price_verified': False,
                     'sku_equivalence_verified': False})
    return rows[:limit], discarded


async def native(action, target, limit=8):
    process = await asyncio.create_subprocess_exec(
        str(HERMES_PYTHON), str(ROOT / 'native.py'), action, target, '--limit', str(limit),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=65)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeError):
        return {'success': False, 'error': 'INVALID_PROVIDER_RESPONSE'}
    if process.returncode or not isinstance(result, dict):
        return {'success': False, 'error': 'PROVIDER_REQUEST_FAILED'}
    return result


async def search_one(query, platform, limit):
    # Exact quoted phrase and per-platform domains reduce unrelated index matches.
    target = 'site:' + DOMAINS[platform] + ' "' + query.replace('"', ' ') + '"'
    try:
        raw = await native('search', target, min(20, max(8, limit * 2)))
        if not raw.get('success'):
            return {'platform': platform, 'status': 'provider_error', 'candidates': [],
                    'error': 'SEARCH_PROVIDER_UNAVAILABLE'}
        rows, discarded = normalize_rows(raw, query, platform, limit)
        return {'platform': platform, 'status': 'ok' if rows else 'no_reliable_index_matches',
                'candidates': rows, 'discarded_unrelated_or_invalid': discarded,
                'note': '未找到索引匹配不代表平台没有商品。' if not rows else NOTE}
    except asyncio.TimeoutError:
        return {'platform': platform, 'status': 'provider_timeout', 'candidates': []}
    except Exception:
        return {'platform': platform, 'status': 'provider_error', 'candidates': []}


async def search_products(query, platforms=None, limit_per_platform=3):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 180 or any(ord(c) < 32 for c in query):
        raise ValueError('QUERY_MUST_BE_1_TO_180_CHARACTERS')
    if re.search(r'(?i)\b(?:https?://|site:)', query):
        raise ValueError('USE_PRODUCT_KEYWORDS_WITHOUT_URL_OR_SITE_OPERATOR')
    platforms = ['jd', 'taobao', 'tmall'] if platforms is None else platforms
    if (not isinstance(platforms, list) or not 1 <= len(platforms) <= 4 or
            any(not isinstance(p, str) or p not in DOMAINS for p in platforms)):
        raise ValueError('PLATFORMS_MUST_BE_JD_TAOBAO_TMALL_OR_DEALS')
    if type(limit_per_platform) is not int or not 1 <= limit_per_platform <= 8:
        raise ValueError('LIMIT_PER_PLATFORM_MUST_BE_1_TO_8')
    platforms = list(dict.fromkeys(platforms))
    results = await asyncio.gather(*(search_one(query.strip(), p, limit_per_platform) for p in platforms))
    found = sum(len(r['candidates']) for r in results)
    successful_requests = [r for r in results if r['status'] in ('ok', 'no_reliable_index_matches')]
    return {'success': bool(successful_requests), 'status': 'matches_found' if found else 'no_verified_candidates',
            'query': query.strip(), 'searched_at': now(), 'backend': 'Hermes existing Exa public web index',
            'platform_native_search': False, 'candidate_count': found, 'platforms': results,
            'manual_search_links': [{'platform': p, 'url': manual_search_url(p, query.strip())} for p in platforms],
            'current_price_verified': False, 'note': NOTE}


def manual_search_url(platform, query):
    if platform == 'jd':
        return 'https://search.jd.com/Search?' + urlencode({'keyword': query, 'enc': 'utf-8'})
    if platform == 'deals':
        return 'https://search.smzdm.com/?' + urlencode({'c': 'home', 's': query})
    return 'https://s.taobao.com/search?' + urlencode({'q': query, **({'tab': 'mall'} if platform == 'tmall' else {})})


async def read_product(url):
    info = parse_product_url(url)
    raw = await native('extract', info['url'])
    data = raw.get('data') if isinstance(raw, dict) else None
    pages = data if isinstance(data, list) else data.get('results', []) if isinstance(data, dict) else []
    if isinstance(data, dict) and any(k in data for k in ('content', 'markdown', 'text')):
        pages = [data]
    selected = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        text = page.get('content') or page.get('markdown') or page.get('text') or ''
        if isinstance(text, str) and text.strip():
            selected.append({'title': str(page.get('title', ''))[:350], 'text': text[:10000]})
    substantial = any(len(p['text']) >= 500 for p in selected)
    return {'success': bool(raw.get('success') and selected), **info,
            'status': 'public_text_extracted' if substantial else 'limited_or_unavailable_page',
            'product_information_verified': False,
            'fetched_at': now(), 'evidence_kind': 'web_extraction_not_authenticated_session',
            'pages': selected, 'current_price': None, 'current_price_verified': False,
            'visible_amount_mentions': [m for p in selected for m in amount_mentions(p['text'])][:8],
            'note': ('正文可能是缓存或登录提示，并非登录账户下的实时商品报价；'
                     '未提供正文时只能返回链接和商品ID，不得编造规格、价格、评价。')}


async def main():
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    server = Server('shopping-search')
    import os
    wishlist = Wishlist(os.environ.get('SHOPPING_WISHLIST_DB', str(DATA_DIR / 'wishlist.sqlite3')), parse_product_url)
    prices = PriceStore(os.environ.get('SHOPPING_PRICE_DB', str(DATA_DIR / 'prices.sqlite3')), parse_product_url)
    @server.list_tools()
    async def list_tools():
        return [
            Tool(name='shopping_search_products',
                 description='搜索京东、淘宝、天猫公开商品网页索引，或什么值得买优惠线索。返回相关标题、链接、摘要；不是平台原生搜索，不能验证实时价格/库存。没有结果不代表无商品。',
                 inputSchema={'type': 'object', 'properties': {
                     'query': {'type': 'string', 'minLength': 1, 'maxLength': 180},
                     'platforms': {'type': 'array', 'minItems': 1, 'maxItems': 4,
                                   'items': {'type': 'string', 'enum': list(DOMAINS)},
                                   'default': ['jd', 'taobao', 'tmall']},
                     'limit_per_platform': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'default': 3}},
                     'required': ['query'], 'additionalProperties': False}),
            Tool(name='shopping_read_product',
                 description='尝试读取完整京东/淘宝/天猫商品链接的公开正文；不使用登录Cookie，不保证正文完整或实时价格，可能仅有登录/首页提示。仅接受完整官方商品URL。',
                 inputSchema={'type': 'object', 'properties': {'url': {'type': 'string'}},
                              'required': ['url'], 'additionalProperties': False}),
            Tool(name='shopping_parse_product_link',
                 description='本地解析京东/淘宝/天猫完整商品链接，去掉追踪参数，返回平台、商品ID、规范链接；不联网、不证明商品存在。',
                 inputSchema={'type': 'object', 'properties': {'url': {'type': 'string'}},
                              'required': ['url'], 'additionalProperties': False}),
            Tool(name='shopping_research_product',
                 description='检索某型号的公开参考价、历史优惠、测评和使用反馈。返回金额上下文和来源链接；不是平台买家评论，不能声称现价、好评率或已验证购买。',
                 inputSchema={'type':'object','properties':{
                     'query':{'type':'string','minLength':1,'maxLength':120},
                     'limit':{'type':'integer','minimum':1,'maximum':4,'default':3},
                     'review_sites':{'type':'array','minItems':1,'maxItems':2,
                                     'items':{'type':'string','enum':['smzdm','zhihu','bilibili','v2ex']},
                                     'default':['smzdm','zhihu']}},'required':['query'],'additionalProperties':False}),
            Tool(name='shopping_compare_products',
                 description='为2至4款不同商品型号汇总公开参考价与测评证据，供按优缺点/用途对比；当前价格未验证，不给最低价赢家。来源有广告与样本偏差，不代表平台评价统计。',
                 inputSchema={'type':'object','properties':{
                     'products':{'type':'array','minItems':2,'maxItems':4,'items':{'type':'string'}},
                     'limit_per_source':{'type':'integer','minimum':1,'maximum':4,'default':2},
                     'review_sites':{'type':'array','minItems':1,'maxItems':2,
                                     'items':{'type':'string','enum':['smzdm','zhihu','bilibili','v2ex']},
                                     'default':['smzdm','zhihu']}},'required':['products'],'additionalProperties':False}),
            Tool(name='shopping_compare_quotes',
                 description='计算调用方提供的同商品、同规格、数量、成色、购买资格、配送条件下15分钟内结算实付报价。不是自动抓价；无合格报价则拒绝选最低。总价必须包含运费及已应用优惠。',
                 inputSchema={'type':'object','properties':{'quotes':{'type':'array','minItems':2,'maxItems':4,
                     'items':{'type':'object','properties':{
                         'url':{'type':'string'},'model':{'type':'string'},'spec':{'type':'string'},
                         'condition':{'type':'string','description':'全新/二手/翻新等成色'},
                         'quantity':{'type':'integer','minimum':1,'maximum':100},
                         'buyer_context':{'type':'string','description':'相同会员/补贴/优惠资格的非敏感代号'},
                         'delivery_context':{'type':'string','description':'相同收货及配送条件的非敏感代号，不传完整地址'},
                         'currency':{'type':'string','enum':['CNY']},
                         'stage':{'type':'string','enum':['checkout_preview','listed_price']},
                         'payable_total':{'type':'number','minimum':0},
                         'observed_at':{'type':'string','description':'真实取得结算报价的ISO时间，含时区'}},
                     'required':['url','model','spec','condition','quantity','buyer_context','delivery_context','currency','stage','payable_total','observed_at'],
                     'additionalProperties':False}}},'required':['quotes'],'additionalProperties':False})] + [Tool(**d) for d in tool_descriptors() + commerce_tool_descriptors()]
    @server.call_tool()
    async def call_tool(name, args):
        try:
            if name == 'shopping_search_products':
                result = await search_products(**args)
            elif name == 'shopping_read_product':
                result = await read_product(**args)
            elif name == 'shopping_parse_product_link':
                result = {'success': True, **parse_product_url(**args), 'existence_verified': False}
            elif name == 'shopping_research_product':
                result = await research_product(fetch=native, **args)
            elif name == 'shopping_compare_products':
                result = await compare_products(fetch=native, **args)
            elif name == 'shopping_compare_quotes':
                result = compare_quotes(normalize_url=parse_product_url, **args)
            elif name == 'shopping_check_variants':
                result = check_variants(normalize_url=parse_product_url, **args)
            elif name == 'shopping_review_digest':
                result = await review_digest(fetch=native, research=research_product, **args)
            elif name == 'shopping_save_item':
                result = wishlist.save(**args)
            elif name == 'shopping_get_list':
                result = wishlist.get(**args)
            elif name == 'shopping_set_budget':
                result = wishlist.set_budget(**args)
            elif name == 'shopping_compare_sellers':
                result = await compare_sellers(fetch=native, read_product=read_product, normalize_url=parse_product_url, **args)
            elif name == 'shopping_calculate_checkout':
                result = calculate_checkout(normalize_url=parse_product_url, **args)
            elif name == 'shopping_record_price':
                result = prices.record(**args)
            elif name == 'shopping_price_history':
                options = dict(args)
                refresh = options.pop('refresh_public_references', False)
                if type(refresh) is not bool:
                    raise ValueError('INVALID_REFRESH_OPTION')
                result = await prices.refresh_history(fetch=native, research=research_product, **options) if refresh else prices.history(**options)
            elif name == 'shopping_price_watch':
                result = prices.watch(**args)
            elif name == 'shopping_check_price_watches':
                result = await prices.check_watches(fetch=native, research=research_product, **args)
            elif name == 'shopping_price_alerts':
                result = prices.alerts(**args)
            else:
                result = {'success': False, 'error': 'UNKNOWN_TOOL'}
        except ValueError as e:
            result = {'success': False, 'error': str(e)}
        except asyncio.TimeoutError:
            result = {'success': False, 'error': 'PROVIDER_TIMEOUT'}
        except Exception:
            result = {'success': False, 'error': 'SHOPPING_REQUEST_FAILED'}
        return [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def cli():
    asyncio.run(main())


if __name__ == '__main__':
    cli()
