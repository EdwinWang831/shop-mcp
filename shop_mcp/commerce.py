"""Source-labeled seller evidence, checkout arithmetic, and persistent price signals."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

from .assistant import money, norm, now, schema, text
from .evidence import model_matches, valid_source

CONTEXT = ('model', 'spec', 'condition', 'quantity', 'buyer_context', 'delivery_context', 'currency')
FACETS = {'店铺身份': ('自营', '旗舰店', '专营店', '店铺', '卖家'),
          '退换货': ('退货', '换货', '无理由', '运费险', '退换'),
          '保修': ('保修', '质保', '维修'), '发票': ('发票', '开票'),
          '配送': ('配送', '运费', '包邮', '发货')}


def date(value):
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if d.tzinfo is None or (d - datetime.now(timezone.utc)).total_seconds() > 60:
            raise ValueError()
        return d
    except (AttributeError, ValueError, TypeError):
        raise ValueError('INVALID_OBSERVED_AT_WITH_TIMEZONE')


def context(value):
    if not isinstance(value, dict):
        raise ValueError('INVALID_QUOTE_CONTEXT')
    out = {k: text(value.get(k), k, 200) for k in CONTEXT if k != 'quantity'}
    if any(norm(v) in ('unknown', '未知', '不详') for v in out.values()):
        raise ValueError('QUOTE_CONTEXT_MUST_BE_KNOWN')
    if out['currency'] != 'CNY' or type(value.get('quantity')) is not int or not 1 <= value['quantity'] <= 100:
        raise ValueError('REQUIRE_CNY_AND_VALID_QUANTITY')
    out['quantity'] = value['quantity']
    return out


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def context_key(value):
    return key({k: norm(v) if isinstance(v, str) else v for k, v in context(value).items()})


def calculate_checkout(quote, normalize_url):
    if not isinstance(quote, dict):
        raise ValueError('INVALID_QUOTE')
    info, ctx = normalize_url(quote.get('url')), context(quote)
    observed = date(quote.get('observed_at'))
    basis = quote.get('price_basis')
    if basis not in ('before_discounts', 'already_discounted') or quote.get('stage') not in ('checkout_preview', 'estimate'):
        raise ValueError('SPECIFY_PRICE_BASIS_AND_STAGE')
    if quote.get('item_subtotal') is None or quote.get('shipping_total') is None:
        return {'success': False, 'error': 'MISSING_ITEM_SUBTOTAL_OR_SHIPPING', 'payable_total': None,
                'note': '商品小计或运费缺失，不能默认运费为0。'}
    subtotal = Decimal(money(quote['item_subtotal'], 'item_subtotal'))
    shipping = Decimal(money(quote['shipping_total'], 'shipping_total'))
    discounts = quote.get('discounts', [])
    if not isinstance(discounts, list) or len(discounts) > 12:
        raise ValueError('INVALID_DISCOUNTS')
    applied, excluded, groups, labels = [], [], set(), set()
    for d in discounts:
        if not isinstance(d, dict):
            raise ValueError('INVALID_DISCOUNT')
        label = text(d.get('label'), 'discount_label', 100)
        amount = money(d.get('amount'), 'discount_amount')
        if amount is None or type(d.get('applied')) is not bool or d.get('eligibility') not in ('confirmed', 'unknown', 'ineligible'):
            raise ValueError('DISCOUNT_NEEDS_AMOUNT_APPLIED_AND_ELIGIBILITY')
        kind = d.get('kind')
        if kind not in ('instant', 'post_purchase_rebate'):
            raise ValueError('INVALID_DISCOUNT_KIND')
        if d['applied'] and d['eligibility'] != 'confirmed':
            raise ValueError('APPLIED_DISCOUNT_ELIGIBILITY_UNVERIFIED')
        if norm(label) in labels:
            raise ValueError('DUPLICATE_DISCOUNT_LABEL')
        labels.add(norm(label))
        if not d['applied'] or d['eligibility'] != 'confirmed' or kind != 'instant':
            excluded.append({'label': label, 'amount': amount, 'reason': 'NOT_CONFIRMED_APPLIED_INSTANT_DISCOUNT'})
            continue
        if basis == 'already_discounted':
            raise ValueError('DOUBLE_DISCOUNT_RISK_USE_BEFORE_DISCOUNTS_OR_EMPTY_APPLIED_DISCOUNTS')
        group = text(d.get('exclusive_group', ''), 'exclusive_group', 100, empty=True)
        if group and group in groups:
            raise ValueError('MUTUALLY_EXCLUSIVE_DISCOUNTS')
        if group:
            groups.add(group)
        applied.append({'label': label, 'amount': amount})
    total = subtotal + shipping - sum((Decimal(d['amount']) for d in applied), Decimal(0))
    if total < 0 or total > 1000000:
        raise ValueError('INVALID_CALCULATED_TOTAL')
    supplied = quote.get('checkout_preview_total')
    if supplied is not None and Decimal(money(supplied, 'checkout_preview_total')) != total:
        return {'success': False, 'error': 'PREVIEW_TOTAL_MISMATCH', 'calculated_total': format(total, '.2f'),
                'supplied_preview_total': money(supplied, 'checkout_preview_total'), 'note': '结算预览与计算结果不一致，先核对费用或优惠，不能选最低价。'}
    current = 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 900 and quote['stage'] == 'checkout_preview'
    return {'success': True, **info, **ctx, 'observed_at': observed.isoformat(), 'stage': quote['stage'],
            'payable_total': format(total, '.2f'), 'item_subtotal': format(subtotal, '.2f'),
            'shipping_total': format(shipping, '.2f'), 'applied_discounts': applied, 'excluded_discounts': excluded,
            'fresh_checkout_context': current, 'current_price_verified': False, 'independently_fetched': False,
            'source': 'caller_provided_checkout_components',
            'note': '只计算提供的费用，不联网验证。未确认资格、未应用优惠及支付后返现不扣减；已优惠小计不能再次减券。结果可给shopping_compare_quotes，但仍需同规格和同购买配送条件、15分钟内真实结算预览。'}


def excerpts(body):
    result = {}
    lines = [x.strip() for x in re.split(r'[\n。；]+', body) if x.strip()]
    for label, words in FACETS.items():
        result[label] = [line[:500] for line in lines if any(w in line for w in words)][:3]
    return result


async def compare_sellers(offers, fetch, read_product, normalize_url):
    if not isinstance(offers, list) or not 2 <= len(offers) <= 4:
        raise ValueError('REQUIRE_2_TO_4_SELLER_OFFERS')
    prepared = []
    for offer in offers:
        if not isinstance(offer, dict):
            raise ValueError('INVALID_OFFER')
        info = normalize_url(offer.get('url'))
        label = text(offer.get('label'), 'label', 120)
        pasted = offer.get('provided_policy_text', '')
        if not isinstance(pasted, str) or len(pasted) > 6000:
            raise ValueError('INVALID_PROVIDED_POLICY_TEXT')
        prepared.append((info, label, pasted))
    async def item(job):
        info, label, pasted = job
        if pasted:
            body, origin, status = pasted, 'caller_provided_listing_text_unverified', 'provided_text'
        else:
            try:
                data = await read_product(info['url'])
                body = '\n'.join(p['text'] for p in data.get('pages', []))
                origin, status = 'public_page_extraction_not_authenticated', data.get('status')
            except Exception:
                body, origin, status = '', 'public_page_extraction_not_authenticated', 'provider_error'
        facets = excerpts(body)
        # Public extractor can return a generic landing page. It is not listing-specific proof.
        return {**info, 'label': label, 'status': status, 'facet_excerpts': facets,
                'missing_facets': [k for k, v in facets.items() if not v],
                'source': origin, 'listing_specific_verified': False, 'merchant_identity_verified': False,
                'note': '原文关键词线索，不是已核实的店铺承诺；首页、导航及平台通用条款不应归给具体卖家。'}
    domains = set('help.jd.com' if i['platform'] == 'jd' else 'rule.taobao.com' for i, _, _ in prepared)
    async def policy(domain):
        try:
            raw = await fetch('search', 'site:' + domain + ' 退换货 发票 保修 规则', 4)
            web = raw.get('data', {}).get('web', []) if isinstance(raw.get('data'), dict) else []
            rows = [{'title': str(w.get('title', ''))[:300], 'url': w['url'], 'snippet': str(w.get('description', ''))[:800],
                     'scope': 'platform_general_not_specific_listing', 'source_published_at': None}
                    for w in web if isinstance(w, dict) and valid_source(w.get('url'), [domain])][:3]
            return {'domain': domain, 'status': 'ok' if rows else 'no_official_index_evidence', 'sources': rows}
        except Exception:
            return {'domain': domain, 'status': 'provider_error', 'sources': []}
    rows, policies = await asyncio.gather(asyncio.gather(*(item(j) for j in prepared)),
                                         asyncio.gather(*(policy(d) for d in sorted(domains))))
    return {'success': True, 'offers': rows, 'official_general_policy_references': policies,
            'winner': None, 'researched_at': now(),
            'note': '按提供/公开原文比较退换、保修、发票、配送；缺信息列未知。平台通用规则不等于每店每商品适用，未核实店铺资质、真伪或具体承诺，不能宣称某店更可靠。'}


class PriceStore:
    def __init__(self, path, normalize_url):
        self.path, self.normalize_url = Path(path), normalize_url

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, query TEXT, kind TEXT, context_key TEXT, amount TEXT, url TEXT, observed_at TEXT, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS watches(id TEXT PRIMARY KEY, query TEXT, mode TEXT, threshold TEXT, context_key TEXT, payload TEXT, enabled INTEGER, revision INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY, watch_id TEXT, observation_id TEXT, created_at TEXT, payload TEXT, read INTEGER DEFAULT 0)')
            yield db
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def put(self, db, payload):
        kind = payload['kind']
        # One checkout observation per URL/context/actual timestamp; one public claim per URL/amount/query.
        ident = key([kind, payload['query'], payload['context_key'], payload['url'], payload['amount'],
                     payload['observed_at'] if kind == 'caller_checkout_quote' else 'public_claim'])
        db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (ident, norm(payload['query']), kind, payload['context_key'], payload['amount'],
                    payload['url'], payload['observed_at'], json.dumps(payload, ensure_ascii=False)))
        return ident

    def trigger(self, db, observation_id, watch_id=None):
        obs = db.execute('SELECT * FROM observations WHERE id=?', (observation_id,)).fetchone()
        payload = json.loads(obs['payload'])
        watches = db.execute('SELECT * FROM watches WHERE enabled=1' + (' AND id=?' if watch_id else ''), (watch_id,) if watch_id else ()).fetchall()
        fresh = payload['kind'] != 'caller_checkout_quote' or 0 <= (datetime.now(timezone.utc) - date(obs['observed_at'])).total_seconds() <= 900
        created = []
        for w in watches:
            mode = 'checkout_quotes' if payload['kind'] == 'caller_checkout_quote' else 'public_reference'
            if not fresh or w['mode'] != mode or obs['query'] != w['query'] or obs['context_key'] != w['context_key'] or obs['amount'] is None:
                continue
            if Decimal(obs['amount']) > Decimal(w['threshold']):
                continue
            aid = key([w['id'], observation_id])
            event = {'watch_id': w['id'], 'query': payload['query'], 'threshold': w['threshold'], 'amount': obs['amount'],
                     'url': obs['url'], 'kind': mode, 'observed_at': obs['observed_at'],
                     'current_price_verified': False, 'independently_fetched': False,
                     'note': '新发现的公开优惠文字满足参考阈值；发布时间/活动有效性/规格/到手价未核实，不代表实际降价。' if mode == 'public_reference' else '提供的同条件新鲜结算价满足阈值；未独立联网核验，非自动付款。'}
            cur = db.execute('INSERT INTO alerts(id,watch_id,observation_id,created_at,payload) VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                             (aid, w['id'], observation_id, now(), json.dumps(event, ensure_ascii=False)))
            if cur.rowcount:
                created.append({'alert_id': aid, **event})
        return created

    def record(self, query, quote):
        query = text(query, 'query', 120)
        ctx = context(quote); info = self.normalize_url(quote.get('url')); observed = date(quote.get('observed_at'))
        if quote.get('stage') != 'checkout_preview':
            raise ValueError('RECORD_REQUIRES_ACTUAL_CHECKOUT_PREVIEW')
        value = money(quote.get('payable_total'), 'payable_total')
        if value is None:
            raise ValueError('MISSING_PAYABLE_TOTAL')
        payload = {'query': query, 'kind': 'caller_checkout_quote', 'context_key': context_key(ctx),
                   'context': ctx, 'amount': value, 'url': info['url'], 'observed_at': observed.isoformat(),
                   'recorded_at': now(), 'source_published_at': None, 'current_price_verified': False,
                   'origin': 'caller_supplied_actual_checkout_preview_not_independently_verified'}
        with self.connection() as db:
            ident = self.put(db, payload); alerts = self.trigger(db, ident)
        return {'success': True, 'observation_id': ident, 'record': payload, 'new_alerts': alerts}

    def history(self, query, limit=30):
        query = text(query, 'query', 120)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('INVALID_HISTORY_LIMIT')
        with self.connection() as db:
            rows = db.execute('SELECT * FROM observations WHERE query=? ORDER BY observed_at DESC LIMIT ?', (norm(query), limit)).fetchall()
        groups = {}
        references = []
        for row in rows:
            p = {'observation_id': row['id'], **json.loads(row['payload'])}
            if p['kind'] == 'public_reference':
                references.append(p); continue
            groups.setdefault(p['context_key'], []).append(p)
        return {'success': True, 'query': query,
                'checkout_history_groups': [{'context_key': k, 'context': v[0]['context'], 'observations': v,
                      'lowest_recorded_total': min((r['amount'] for r in v), key=Decimal)} for k, v in groups.items()],
                'public_reference_observations': references, 'complete_market_history': False,
                'note': '仅本地已记录历史；结算价按同规格/数量/成色/购买配送条件分组。公开索引观察时间不等于价格生效时间，不能当平台历史最低价。'}

    def watch(self, action, query=None, target_amount=None, mode=None, quote_context=None, watch_id=None, expected_revision=None):
        if action == 'list':
            with self.connection() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM watches ORDER BY id')]
            for r in rows:
                r['details'] = json.loads(r.pop('payload')); r['enabled'] = bool(r['enabled'])
            return {'success': True, 'watches': rows, 'automatic_checks_scheduled': False,
                    'note': '此工具仅管理阈值规则，不创建定时任务。自动检查需Hermes cronjob真实创建并核实调度和投递。'}
        if action not in ('create', 'pause', 'resume'):
            raise ValueError('INVALID_WATCH_ACTION')
        with self.connection() as db:
            if action != 'create':
                row = db.execute('SELECT * FROM watches WHERE id=?', (watch_id,)).fetchone()
                if row is None or type(expected_revision) is not int or row['revision'] != expected_revision:
                    raise ValueError('WATCH_NOT_FOUND_OR_REVISION_CONFLICT')
                db.execute('UPDATE watches SET enabled=?,revision=revision+1 WHERE id=?', (int(action == 'resume'), watch_id))
                return {'success': True, 'watch_id': watch_id, 'enabled': action == 'resume', 'revision': row['revision'] + 1}
            query = text(query, 'query', 120)
            amount = money(target_amount, 'target_amount')
            if amount is None or mode not in ('public_reference', 'checkout_quotes') or re.search(r'https?://|site:', query, re.I):
                raise ValueError('REQUIRE_QUERY_THRESHOLD_AND_EXPLICIT_WATCH_MODE')
            ctx = context(quote_context) if mode == 'checkout_quotes' else None
            ck = context_key(ctx) if ctx else ''
            existing = db.execute('SELECT * FROM watches WHERE query=? AND mode=? AND threshold=? AND context_key=? AND enabled=1',
                                  (norm(query), mode, amount, ck)).fetchone()
            if existing:
                return {'success': True, 'watch_id': existing['id'], 'revision': existing['revision'], 'already_exists': True, 'automatic_checks_scheduled': False}
            ident = str(uuid.uuid4())
            payload = {'query': query, 'quote_context': ctx, 'created_at': now()}
            db.execute('INSERT INTO watches VALUES (?,?,?,?,?,?,1,1)', (ident, norm(query), mode, amount, ck, json.dumps(payload, ensure_ascii=False)))
        return {'success': True, 'watch_id': ident, 'revision': 1, 'mode': mode, 'target_amount': amount,
                'automatic_checks_scheduled': False, 'notification_delivery_configured': False,
                'note': '规则已保存；public_reference是公开优惠线索，checkout_quotes只在提供同条件结算价时触发。尚未创建定时检查或推送。'}

    def reference_candidates(self, query, sources):
        result = []
        for s in sources:
            url, title = s.get('url', ''), str(s.get('title', ''))
            if not valid_source(url, ['smzdm.com']) or not re.fullmatch(r'/p/[0-9]+/?', urlsplit(url).path) or not model_matches(query, title):
                continue
            amounts = re.findall(r'(?<![0-9.])([0-9]{1,7}(?:\.[0-9]{1,2})?)\s*元', title)
            pattern = r'(?i)(?<![a-z0-9])[a-z][a-z0-9_-]*[0-9][a-z0-9_-]*(?![a-z0-9])'
            wanted = {m.casefold() for m in re.findall(pattern, query)}
            seen = {m.casefold() for m in re.findall(pattern, title)}
            risky = bool(seen - wanted) or any(w in title for w in ('配件', '适用', '兼容', '脚贴', '保护套', '收纳盒', '替换', '二手', '翻新', '满减', '优惠券', '券码', '起', '省', '减', '返', '补贴', '抵', '定金', '订金'))
            # Only unambiguous single title amounts can trigger reference hints. Snippet amounts never do.
            amount = money(amounts[0], 'reference_amount') if len(amounts) == 1 and not risky else None
            result.append({'query': query, 'kind': 'public_reference', 'context_key': '', 'amount': amount,
                           'title': title[:500], 'url': url, 'observed_at': now(), 'source_published_at': None,
                           'amount_candidates': amounts, 'current_price_verified': False,
                           'origin': 'public_deal_index_title_not_current_checkout',
                           'note': '检索时见到的标题报价线索；发布时间和有效性未知。多金额、配件或优惠券歧义不触发价格提醒。'})
        return result

    async def refresh_history(self, query, fetch, research, limit=30):
        data = await research(query, fetch, limit=3, review_sites=['smzdm'])
        candidates = self.reference_candidates(data['product_query'], data['price_references'])
        with self.connection() as db:
            for payload in candidates:
                self.put(db, payload)
        result = self.history(query, limit)
        result['public_refresh'] = {'channels': data['channels'], 'recorded_references': len(candidates),
                                    'original_price_sources': data['price_references']}
        return result

    async def check_watches(self, fetch, research, watch_id=None):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM watches WHERE enabled=1' + (' AND id=?' if watch_id else '') + ' LIMIT 6',
                              (watch_id,) if watch_id else ()).fetchall()
        if len(rows) > 5:
            raise ValueError('MORE_THAN_5_ACTIVE_WATCHES_PASS_WATCH_ID')
        async def run(w):
            if w['mode'] != 'public_reference':
                return {'watch_id': w['id'], 'status': 'waiting_for_caller_checkout_quotes', 'new_alerts': []}
            data = await research(json.loads(w['payload'])['query'], fetch, limit=3, review_sites=['smzdm'])
            candidates = self.reference_candidates(data['product_query'], data['price_references'])
            alerts = []
            with self.connection() as db:
                for p in candidates:
                    ident = self.put(db, p); alerts.extend(self.trigger(db, ident, w['id']))
            price_channel = next(c['status'] for c in data['channels'] if c['channel'] == 'prices')
            return {'watch_id': w['id'], 'status': price_channel, 'recorded_references': len(candidates), 'new_alerts': alerts}
        results = await asyncio.gather(*(run(w) for w in rows))
        return {'success': True, 'checked_at': now(), 'checks': results,
                'new_alerts': [a for r in results for a in r['new_alerts']], 'notification_sent': False,
                'note': '只生成本地提醒事件，尚未向任何渠道推送。无新事件时保持安静；公开线索不能作为已核实降价。'}

    def alerts(self, unread_only=True, acknowledge_ids=None, limit=30):
        if type(unread_only) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('INVALID_ALERT_OPTIONS')
        if acknowledge_ids is not None and (not isinstance(acknowledge_ids, list) or len(acknowledge_ids) > 100 or any(not isinstance(i, str) or len(i) != 64 for i in acknowledge_ids)):
            raise ValueError('INVALID_ACKNOWLEDGE_IDS')
        with self.connection() as db:
            for aid in acknowledge_ids or []:
                db.execute('UPDATE alerts SET read=1 WHERE id=?', (aid,))
            rows = db.execute('SELECT * FROM alerts' + (' WHERE read=0' if unread_only else '') + ' ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return {'success': True, 'alerts': [{'alert_id': r['id'], 'created_at': r['created_at'], 'read': bool(r['read']),
                                           **json.loads(r['payload'])} for r in rows], 'notification_sent': False}


def commerce_tool_descriptors():
    short = {'type': 'string', 'minLength': 1, 'maxLength': 200}
    ctx = {**{k: short for k in CONTEXT if k not in ('currency', 'quantity')},
           'currency': {'type': 'string', 'enum': ['CNY']},
           'quantity': {'type': 'integer', 'minimum': 1, 'maximum': 100}}
    amount = {'type': 'number', 'minimum': 0, 'maximum': 1000000}
    query = {'type': 'string', 'minLength': 1, 'maxLength': 120}
    identity = {**ctx, 'url': {'type': 'string'}, 'observed_at': {'type': 'string', 'description': '实际取得报价的ISO时间，必须含时区'},
                'stage': {'type': 'string', 'enum': ['checkout_preview']}}
    discount = schema({'label': short, 'amount': amount, 'applied': {'type': 'boolean'},
                       'eligibility': {'type': 'string', 'enum': ['confirmed', 'unknown', 'ineligible']},
                       'kind': {'type': 'string', 'enum': ['instant', 'post_purchase_rebate']},
                       'exclusive_group': {'type': 'string', 'maxLength': 100}},
                      ['label', 'amount', 'applied', 'eligibility', 'kind'])
    components = {**identity, 'stage': {'type': 'string', 'enum': ['checkout_preview', 'estimate']},
                  'price_basis': {'type': 'string', 'enum': ['before_discounts', 'already_discounted']},
                  'item_subtotal': {'type': ['number', 'null'], 'minimum': 0, 'description': '所有quantity件商品的小计，不是单价'},
                  'shipping_total': {'type': ['number', 'null'], 'minimum': 0, 'description': '总运费，未知传null，不能猜0'},
                  'discounts': {'type': 'array', 'maxItems': 12, 'items': discount},
                  'checkout_preview_total': amount}
    return [
        {'name': 'shopping_compare_sellers', 'description': '对2至4个商品链接或用户粘贴的店铺条款整理退换货、保修、发票、配送、店铺身份原文线索。另查官方平台通用规则，明确不等于具体商品承诺；缺项未知，不给可靠店铺赢家。',
         'inputSchema': schema({'offers': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': schema({
             'label': {'type': 'string', 'minLength': 1, 'maxLength': 120}, 'url': {'type': 'string'},
             'provided_policy_text': {'type': 'string', 'maxLength': 6000, 'description': '用户提供的该商品/店铺条款原文；未提供则尝试公开提取，可能失败。'}}, ['label', 'url'])}}, ['offers'])},
        {'name': 'shopping_calculate_checkout', 'description': '计算提供的商品小计+总运费-已确认且已应用的即时优惠。防重复减券、互斥优惠、未确认资格和返现提前扣款；运费缺失则不给总价。不是自动抓取报价，实际结算预览合格后可交shopping_compare_quotes。',
         'inputSchema': schema({'quote': schema(components, [*identity, 'price_basis', 'item_subtotal', 'shipping_total'])}, ['quote'])},
        {'name': 'shopping_record_price', 'description': '用户明确要求保存实际结算预览报价时，记录本地历史；总价必须含运费和已应用优惠。时间来自实际观察，不用现在时间冒充。支持旧报价作历史，新鲜同条件报价可触发阈值提醒；来源是提供数据，未独立抓取。',
         'inputSchema': schema({'query': query, 'quote': schema({**identity, 'payable_total': amount}, [*identity, 'payable_total'])}, ['query', 'quote'])},
        {'name': 'shopping_price_history', 'description': '读取本地报价历史，按规格/数量/成色/资格/配送分组。refresh_public_references=true时检索并保存公开优惠线索，另列观察时间和未知发布时间；不是平台完整历史或全网历史最低价。',
         'inputSchema': schema({'query': query, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 30},
                               'refresh_public_references': {'type': 'boolean', 'default': False}}, ['query'])},
        {'name': 'shopping_price_watch', 'description': '仅用户要求提醒时管理持久价格阈值规则：create/list/pause/resume。public_reference提醒公开优惠文字，不保证现价；checkout_quotes只基于提供的新鲜同条件结算价，创建需quote_context。仅保存规则，不创建定时任务或推送；自动运行须Hermes cronjob真实创建及核实投递。',
         'inputSchema': schema({'action': {'type': 'string', 'enum': ['create', 'list', 'pause', 'resume']},
             'query': query, 'target_amount': amount, 'mode': {'type': 'string', 'enum': ['public_reference', 'checkout_quotes']},
             'quote_context': schema(ctx, ctx.keys()), 'watch_id': {'type': 'string'},
             'expected_revision': {'type': 'integer', 'minimum': 1}}, ['action'])},
        {'name': 'shopping_check_price_watches', 'description': '实际检查最多5条已启用规则：公开优惠线索通过既有网页搜索检索并留历史；结算规则等用户提供报价。生成本地新提醒并去重，无事件不代表价格没变；本工具不向渠道发送消息。可用于用户授权的Hermes定时任务。',
         'inputSchema': schema({'watch_id': {'type': 'string'}})},
        {'name': 'shopping_price_alerts', 'description': '读取本地阈值提醒，明确公开线索/提供的结算价，均未经独立实时核验；仅用户已读或推送成功后才传acknowledge_ids标记。提醒存在不证明渠道已送达。',
         'inputSchema': schema({'unread_only': {'type': 'boolean', 'default': True},
             'acknowledge_ids': {'type': 'array', 'maxItems': 100, 'items': {'type': 'string', 'minLength': 64, 'maxLength': 64}},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 30}})},
    ]
