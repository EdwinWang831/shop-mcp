"""Shopping comparison evidence; indexed amounts are never current quotes."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

REVIEW_SITES = {'smzdm': 'post.smzdm.com', 'zhihu': 'zhihu.com',
                'bilibili': 'bilibili.com', 'v2ex': 'v2ex.com'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def amount_mentions(text):
    """Return monetary text with context, including coupons and historical prices."""
    pattern = r'(?:[￥¥]\s*([0-9]{1,7}(?:\.[0-9]{1,2})?)(?![0-9.])|(?<![0-9.])([0-9]{1,7}(?:\.[0-9]{1,2})?)\s*元)'
    result, seen = [], set()
    for match in re.finditer(pattern, text):
        value = match.group(1) or match.group(2)
        if Decimal(value) <= 0 or value in seen:
            continue
        seen.add(value)
        result.append({'amount_text': value, 'currency': None, 'currency_verified': False,
                       'context': text[max(0, match.start()-45):min(len(text), match.end()+65)],
                       'price_kind': 'unverified_monetary_mention', 'current_price_verified': False})
        if len(result) == 5:
            break
    return result


def valid_source(url, domains):
    try:
        u = urlsplit(url)
        if u.scheme != 'https' or u.username or u.password or u.port:
            return False
        return any(u.hostname == d or (u.hostname or '').endswith('.' + d) for d in domains)
    except (TypeError, ValueError):
        return False


def model_matches(query, text):
    models = re.findall(r'(?i)(?<![a-z0-9])[a-z][a-z0-9_-]*[0-9][a-z0-9_-]*(?![a-z0-9])', query)
    if models:
        return all(re.search(r'(?<![a-z0-9])' + re.escape(m) + r'(?![a-z0-9])', text, re.I) for m in models)
    terms = re.findall(r'[a-zA-Z0-9_-]{2,}|[\u4e00-\u9fff]{2,}', query)
    return any(t.casefold() in text.casefold() for t in terms)


def scoped_snippet(query, title, snippet):
    """Avoid attributing feedback about another model to the requested product."""
    pattern = r'(?i)(?<![a-z0-9])[a-z][a-z0-9_-]*[0-9][a-z0-9_-]*(?![a-z0-9])'
    wanted = {m.casefold() for m in re.findall(pattern, query)}
    if not wanted:
        return snippet[:1200], 'attribution_needs_manual_check'
    seen = {m.casefold() for m in re.findall(pattern, snippet)}
    mixed = seen - wanted
    if not mixed and model_matches(query, title):
        return snippet[:1200], 'target_model_in_title'
    segments = re.split(r'\s*\.{3,}\s*|[\n。！？；]+', snippet)
    focused = []
    for segment in segments:
        mentioned = {m.casefold() for m in re.findall(pattern, segment)}
        if wanted <= mentioned and not mentioned - wanted:
            focused.append(segment.strip())
    return '\n'.join(focused)[:1200], 'explicit_target_model_segments_only'


def sources(raw, query, domains, limit, kind):
    web = raw.get('data', {}).get('web', []) if isinstance(raw.get('data'), dict) else []
    rows, seen = [], set()
    for row in web if isinstance(web, list) else []:
        if not isinstance(row, dict):
            continue
        url, title, snippet = row.get('url', ''), str(row.get('title', '')), str(row.get('description', ''))
        if not valid_source(url, domains) or url in seen or not model_matches(query, title + ' ' + snippet):
            continue
        snippet, scope = scoped_snippet(query, title, snippet)
        if kind == 'public_review_index' and not snippet.strip():
            continue
        seen.add(url)
        text = (title + '\n' + snippet)[:1800]
        rows.append({'title': title[:300], 'url': url, 'snippet': snippet,
                     'attribution_scope': scope,
                     'attribution_note': '片段可能缺上下文；不得将其他型号、其他品类或评论区中的个例反馈归给本商品。',
                     'source_kind': kind, 'source_published_at': None, 'retrieved_at': timestamp(),
                     'full_article_read': False, 'purchase_verified': False,
                     'monetary_mentions': amount_mentions(text) if kind == 'price_reference_index' else []})
        if len(rows) == limit:
            break
    return rows


async def research_product(query, fetch, limit=3, review_sites=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120 or any(ord(c) < 32 for c in query):
        raise ValueError('PRODUCT_QUERY_MUST_BE_1_TO_120_CHARACTERS')
    if re.search(r'(?i)\b(?:https?://|site:)', query):
        raise ValueError('USE_PRODUCT_MODEL_WITHOUT_URL_OR_SITE_OPERATOR')
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError('LIMIT_MUST_BE_1_TO_4')
    review_sites = ['smzdm', 'zhihu'] if review_sites is None else review_sites
    if (not isinstance(review_sites, list) or not 1 <= len(review_sites) <= 2 or
            any(not isinstance(x, str) or x not in REVIEW_SITES for x in review_sites)):
        raise ValueError('SELECT_1_TO_2_REVIEW_SITES')
    query = query.strip()
    jobs = [('prices', query + ' 价格 优惠 site:smzdm.com', ['smzdm.com'], 'price_reference_index')]
    jobs += [(site, query + ' 优点 缺点 使用体验 site:' + REVIEW_SITES[site],
              [REVIEW_SITES[site]], 'public_review_index') for site in dict.fromkeys(review_sites)]
    async def run(job):
        label, target, domains, kind = job
        try:
            raw = await fetch('search', target, max(5, limit * 2))
            found = sources(raw, query, domains, limit, kind) if raw.get('success') else []
            return {'channel': label, 'status': 'ok' if found else 'no_reliable_index_evidence' if raw.get('success') else 'provider_error',
                    'sources': found}
        except asyncio.TimeoutError:
            return {'channel': label, 'status': 'provider_timeout', 'sources': []}
        except Exception:
            return {'channel': label, 'status': 'provider_error', 'sources': []}
    channels = await asyncio.gather(*(run(job) for job in jobs))
    price_sources = channels[0]['sources']
    reviews = [s for c in channels[1:] for s in c['sources']]
    return {'success': bool(price_sources or reviews), 'product_query': query,
            'researched_at': timestamp(), 'price_references': price_sources,
            'public_reviews': reviews, 'channels': [{'channel': c['channel'], 'status': c['status']} for c in channels],
            'current_price': None, 'current_price_verified': False,
            'platform_buyer_reviews_available': False, 'platform_rating': None,
            'note': '金额是网页索引中的文字线索，可能是历史成交、起价、优惠券或旧活动，不能视为现价。公开测评并非平台已购评价；来源可能有广告或个体偏差，检索时间不是文章发布时间。'}


async def compare_products(products, fetch, limit_per_source=2, review_sites=None):
    if (not isinstance(products, list) or not 2 <= len(products) <= 4 or
            any(not isinstance(p, str) for p in products)):
        raise ValueError('REQUIRE_2_TO_4_PRODUCT_MODEL_STRINGS')
    if len(set(p.strip().casefold() for p in products)) != len(products):
        raise ValueError('USE_DISTINCT_PRODUCT_QUERIES')
    for p in products:
        if not 1 <= len(p.strip()) <= 120 or any(ord(c) < 32 for c in p) or re.search(r'(?i)\b(?:https?://|site:)', p):
            raise ValueError('INVALID_PRODUCT_MODEL_QUERY')
    # research_product also validates options before its first retrieval.
    results = await asyncio.gather(*(research_product(p, fetch, limit_per_source, review_sites) for p in products))
    return {'success': any(r['success'] for r in results), 'products': results,
            'comparison_kind': 'public_evidence_matrix', 'current_price_comparable': False,
            'lowest_price_winner': None,
            'note': '可按带来源的公开测评讨论优缺点和适用场景；没有当前同条件报价，不能选出最便宜平台。金额缺失不表示免费、缺货或更贵。'}


def compare_quotes(quotes, normalize_url, checked_at=None):
    """Calculate caller-supplied checkout quotes, without claiming independent fetching."""
    if not isinstance(quotes, list) or not 2 <= len(quotes) <= 4:
        raise ValueError('REQUIRE_2_TO_4_QUOTES')
    checked_at = checked_at or datetime.now(timezone.utc)
    rows, keys, reasons = [], [], []
    for i, quote in enumerate(quotes):
        try:
            if not isinstance(quote, dict):
                raise ValueError()
            info = normalize_url(quote['url'])
            fields = ('model', 'spec', 'condition', 'buyer_context', 'delivery_context', 'currency')
            values = tuple(quote[k].strip().casefold() for k in fields)
            if not all(values) or values[-1] != 'cny':
                raise ValueError()
            quantity = quote['quantity']
            if type(quantity) is not int or not 1 <= quantity <= 100:
                raise ValueError()
            keys.append(values + (quantity,))
            if quote['stage'] != 'checkout_preview':
                reasons.append(f'quote_{i}:NOT_CHECKOUT_PRICE')
            date = datetime.fromisoformat(quote['observed_at'].replace('Z', '+00:00'))
            if date.tzinfo is None or not -60 <= (checked_at-date).total_seconds() <= 900:
                reasons.append(f'quote_{i}:STALE_OR_INVALID_TIMESTAMP')
            value = Decimal(str(quote['payable_total']))
            if not value.is_finite() or not 0 <= value <= 1000000:
                raise ValueError()
            rows.append({**info, 'payable_total': str(value), 'quantity': quantity,
                         'observed_at': quote['observed_at']})
        except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
            reasons.append(f'quote_{i}:MISSING_OR_INVALID_QUOTE')
    if keys and any(k != keys[0] for k in keys):
        reasons.append('MODEL_SPEC_CONDITION_QUANTITY_BUYER_DELIVERY_OR_CURRENCY_MISMATCH')
    if reasons:
        return {'success': False, 'comparable': False, 'reasons': reasons, 'lowest_quotes': [],
                'independently_fetched': False}
    rows.sort(key=lambda r: Decimal(r['payable_total']))
    lowest = Decimal(rows[0]['payable_total'])
    return {'success': True, 'comparable': True, 'quotes': rows,
            'lowest_quotes': [r for r in rows if Decimal(r['payable_total']) == lowest],
            'saving_vs_highest': str(Decimal(rows[-1]['payable_total']) - lowest),
            'independently_fetched': False, 'source': 'caller_provided_checkout_quotes',
            'note': '仅计算调用方提供的同型号、规格、数量、成色、购买资格、配送条件下15分钟内结算实付总价，包含运费及已应用优惠；未独立联网验证报价或优惠资格。'}
