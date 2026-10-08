"""Conservative variant checks, source quotations, and a durable personal wishlist."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

ATTRS = ('brand', 'model', 'spec', 'condition', 'item_kind', 'quantity')
KINDS = ('full_product', 'accessory', 'bundle', 'unknown')
STATUSES = ('planned', 'purchased', 'archived')
TOPICS = {
    '舒适与尺寸': ('舒适', '舒服', '握持', '手感', '硌手', '手大', '小手', '尺寸'),
    '可靠性': ('双击', '故障', '耐用', '失灵', '损坏', '断连', '可靠'),
    '性能与连接': ('延迟', '响应', '精度', '性能', '连接', '蓝牙', '信号'),
    '续航与重量': ('电池', '续航', '充电', '重量', '轻', '重'),
    '做工与声音': ('做工', '材质', '声音', '噪音', '静音', '按键'),
    '售后体验': ('退货', '换货', '售后', '保修'),
}
POSITIVE = ('舒服', '舒适', '稳定', '耐用', '流畅', '精准', '静音', '不累')
NEGATIVE = ('不舒服', '不舒适', '不稳定', '不耐用', '不精准', '不静音', '故障', '双击', '失灵', '断连', '硌手')


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, field, limit=300, empty=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('INVALID_' + field.upper())
    value = value.strip()
    if not value and not empty:
        raise ValueError('EMPTY_' + field.upper())
    return value


def norm(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).strip().casefold())


def money(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('INVALID_' + field.upper())
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= 1000000 or result != result.quantize(Decimal('.01')):
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError('INVALID_' + field.upper())
    return format(result, '.2f')


def attributes(value):
    if not isinstance(value, dict) or set(value) - set(ATTRS):
        raise ValueError('INVALID_ATTRIBUTES')
    out = {}
    for k, v in value.items():
        if k == 'quantity':
            if type(v) is not int or not 1 <= v <= 100:
                raise ValueError('INVALID_QUANTITY')
            out[k] = v
        else:
            out[k] = text(v, k, empty=True)
            if k == 'item_kind' and v not in KINDS:
                raise ValueError('INVALID_ITEM_KIND')
    return out


def check_variants(target, candidates, normalize_url):
    target = attributes(target)
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ValueError('REQUIRE_1_TO_8_CANDIDATES')
    rows = []
    for c in candidates:
        if not isinstance(c, dict) or set(c) - {'label', 'title', 'url', 'attributes'}:
            raise ValueError('INVALID_CANDIDATE')
        label = text(c.get('label'), 'label', 120)
        title = text(c.get('title', ''), 'title', 500, empty=True)
        actual = attributes(c.get('attributes', {}))
        url = normalize_url(c['url'])['url'] if c.get('url') else None
        missing, conflicts = [], []
        for field in ATTRS:
            a, b = target.get(field), actual.get(field)
            unknown = lambda v: v is None or (isinstance(v, str) and norm(v) in ('', 'unknown', '未知', '不详', '未提供'))
            if unknown(a) or unknown(b):
                missing.append(field)
            elif (a if field == 'quantity' else norm(a)) != (b if field == 'quantity' else norm(b)):
                conflicts.append({'field': field, 'target': a, 'candidate': b})
        hints = [word for word in ('配件', '适用', '兼容', '替换', '保护套', '收纳盒', '脚贴', '二手', '翻新', '套装', '升级款') if word in title]
        model_tokens = re.findall(r'(?i)(?<![a-z0-9])[a-z][a-z0-9_-]*[0-9][a-z0-9_-]*(?![a-z0-9])', actual.get('model', ''))
        if title and model_tokens and not all(re.search(r'(?<![a-z0-9])' + re.escape(m) + r'(?![a-z0-9])', title, re.I) for m in model_tokens):
            hints.append('标题未确认所提供型号')
        # Do not silently trust caller fields contradicting an accessory-like title.
        status = 'different_declared_attributes' if conflicts else 'insufficient_information' if missing or hints else 'matching_declared_attributes'
        rows.append({'label': label, 'url': url, 'status': status, 'conflicts': conflicts,
                     'missing_fields': missing, 'title_risk_hints': hints,
                     'candidate_attributes': actual, 'sku_equivalence_verified': False})
    return {'success': True, 'target': target, 'candidates': rows,
            'attribute_origin': 'caller_supplied_not_independently_verified',
            'note': '仅核对提供的字段；同型号或同链接不证明同SKU。规格应包含容量、颜色、代际、套装等购买相关选项。缺项、配件或升级款提示须核实；字段相符也不是平台认证的同款。'}


def polarity_cues(excerpt):
    negatives = [w for w in NEGATIVE if w in excerpt]
    cleaned = excerpt
    for word in negatives:
        cleaned = cleaned.replace(word, '')
    positives = [w for w in POSITIVE if w in cleaned]
    # These are quoted cue words, not semantic sentiment decisions.
    return {'positive_words': positives, 'negative_words': negatives,
            'semantics_verified': False}


def digest_evidence(research):
    sources = research['public_reviews']
    topics = []
    for label, words in TOPICS.items():
        statements = []
        for source in sources:
            # Existing research attribution filter has already restricted mixed-model excerpts.
            for segment in re.split(r'[\n。！？；]+', source['snippet']):
                if any(w in segment for w in words) and segment.strip():
                    statements.append({'excerpt': segment.strip()[:500], 'url': source['url'],
                                       'source_title': source['title'],
                                       'source_published_at': source['source_published_at'],
                                       'full_article_read': source['full_article_read'],
                                       'purchase_verified': source['purchase_verified'],
                                       'cue_words': polarity_cues(segment)})
        if not statements:
            continue
        pos = {s['url'] for s in statements if s['cue_words']['positive_words']}
        neg = {s['url'] for s in statements if s['cue_words']['negative_words']}
        topics.append({'topic': label, 'statements': statements[:12],
                       'possible_opposing_wording': any(a != b for a in pos for b in neg),
                       'conflict_verified': False})
    return {'success': bool(sources), 'product_query': research['product_query'],
            'topics': topics, 'sources': sources, 'channels': research['channels'],
            'unclassified_sources': [s['url'] for s in sources if not any(s['url'] == q['url'] for t in topics for q in t['statements'])],
            'source_count': len({s['url'] for s in sources}), 'retrieved_at': now(),
            'platform_buyer_reviews_available': False, 'platform_rating': None,
            'method': 'keyword_grouped_verbatim_index_excerpts',
            'note': '按主题保留公开索引原文片段及来源；关键词仅是待核对线索，不是语义验证、平台已购评价或故障率统计。可能相反的措辞须结合原文、型号、时间和使用场景判断；来源发布时间未知不得以检索时间替代。'}


async def review_digest(query, fetch, research, limit=3, review_sites=None):
    return digest_evidence(await research(query, fetch, limit, review_sites))


class Wishlist:
    """Instance-local personal lists; no account/session identity is inferred."""
    def __init__(self, path, normalize_url):
        self.path = Path(path)
        self.normalize_url = normalize_url

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS lists(name TEXT PRIMARY KEY, budget TEXT, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, list_name TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_name(self, value):
        return text(value, 'list_name', 80)

    def validate_patch(self, patch):
        allowed = {'title', 'product_query', 'spec', 'use_case', 'preferences', 'quantity', 'unit_budget', 'status', 'urls'}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise ValueError('INVALID_ITEM_FIELDS')
        out = {}
        for field, value in patch.items():
            if field in ('title', 'product_query', 'spec', 'use_case'):
                out[field] = text(value, field, 300, empty=field in ('spec', 'use_case'))
            elif field == 'preferences':
                if not isinstance(value, list) or len(value) > 10:
                    raise ValueError('INVALID_PREFERENCES')
                out[field] = [text(p, 'preference', 200) for p in value]
            elif field == 'urls':
                if not isinstance(value, list) or len(value) > 8:
                    raise ValueError('INVALID_URLS')
                out[field] = list(dict.fromkeys(self.normalize_url(u)['url'] for u in value))
            elif field == 'quantity':
                if type(value) is not int or not 1 <= value <= 100:
                    raise ValueError('INVALID_QUANTITY')
                out[field] = value
            elif field == 'unit_budget':
                out[field] = money(value, field)
            elif field == 'status':
                if value not in STATUSES:
                    raise ValueError('INVALID_STATUS')
                out[field] = value
        return out

    def row(self, value):
        return {'item_id': value['id'], 'revision': value['revision'],
                'created_at': value['created_at'], 'updated_at': value['updated_at'],
                **json.loads(value['payload'])}

    def save(self, item, list_name='默认', item_id=None, expected_revision=None):
        name = self.list_name(list_name)
        patch = self.validate_patch(item)
        if item_id is not None:
            item_id = text(item_id, 'item_id', 36)
            if type(expected_revision) is not int or expected_revision < 1:
                raise ValueError('UPDATE_REQUIRES_EXPECTED_REVISION_FROM_LIST')
        elif expected_revision is not None:
            raise ValueError('NEW_ITEM_HAS_NO_EXPECTED_REVISION')
        with self.connection() as db:
            if item_id:
                previous = db.execute('SELECT * FROM items WHERE id=? AND list_name=?', (item_id, name)).fetchone()
                if previous is None:
                    raise ValueError('ITEM_NOT_FOUND')
                if previous['revision'] != expected_revision:
                    raise ValueError('REVISION_CONFLICT_RELOAD_LIST')
                payload = {**json.loads(previous['payload']), **patch}
                revision, created = previous['revision'] + 1, previous['created_at']
            else:
                if not patch.get('title') or not patch.get('product_query'):
                    raise ValueError('NEW_ITEM_REQUIRES_TITLE_AND_PRODUCT_QUERY')
                if db.execute('SELECT count(*) FROM items WHERE list_name=?', (name,)).fetchone()[0] >= 500:
                    raise ValueError('LIST_ITEM_LIMIT_500')
                for row in db.execute('SELECT * FROM items WHERE list_name=?', (name,)):
                    saved = json.loads(row['payload'])
                    if saved['status'] != 'archived' and norm(saved['title']) == norm(patch['title']):
                        return {'success': False, 'error': 'DUPLICATE_TITLE_UPDATE_EXISTING_ITEM', 'existing_item_id': row['id'], 'revision': row['revision']}
                item_id, revision, created = str(uuid.uuid4()), 1, now()
                payload = {'spec': '', 'use_case': '', 'preferences': [], 'quantity': 1,
                           'unit_budget': None, 'status': 'planned', 'urls': [], **patch}
            updated = now()
            db.execute('INSERT INTO lists VALUES (?,NULL,1,?) ON CONFLICT(name) DO NOTHING', (name, updated))
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at',
                       (item_id, name, json.dumps(payload, ensure_ascii=False), revision, created, updated))
            result = self.row(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())
        return {'success': True, 'list_name': name, 'item': result, 'currency': 'CNY',
                'storage_scope': 'personal_Hermes_instance_not_chat_isolated',
                'note': '预算是用户计划，不是现价或成交价；只有用户明确要求时才保存/修改。'}

    def set_budget(self, total_budget, list_name='默认', expected_revision=None):
        name, budget = self.list_name(list_name), money(total_budget, 'total_budget')
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError('INVALID_EXPECTED_REVISION')
        with self.connection() as db:
            previous = db.execute('SELECT * FROM lists WHERE name=?', (name,)).fetchone()
            if previous and previous['revision'] != expected_revision:
                raise ValueError('REVISION_CONFLICT_RELOAD_LIST')
            if not previous and expected_revision is not None:
                raise ValueError('LIST_NOT_FOUND')
            revision = previous['revision'] + 1 if previous else 1
            db.execute('INSERT INTO lists VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET budget=excluded.budget,revision=excluded.revision,updated_at=excluded.updated_at',
                       (name, budget, revision, now()))
        return {'success': True, 'list_name': name, 'total_budget': budget, 'revision': revision, 'currency': 'CNY'}

    def get(self, list_name='默认', include_inactive=False):
        name = self.list_name(list_name)
        if type(include_inactive) is not bool:
            raise ValueError('INVALID_INCLUDE_INACTIVE')
        with self.connection() as db:
            setting = db.execute('SELECT * FROM lists WHERE name=?', (name,)).fetchone()
            all_items = [self.row(r) for r in db.execute('SELECT * FROM items WHERE list_name=? ORDER BY created_at,id', (name,))]
        planned = [i for i in all_items if i['status'] == 'planned']
        missing = [i['item_id'] for i in planned if i['unit_budget'] is None]
        allocated = sum((Decimal(i['unit_budget']) * i['quantity'] for i in planned if i['unit_budget'] is not None), Decimal(0))
        budget = setting['budget'] if setting else None
        remaining = Decimal(budget) - allocated if budget is not None else None
        return {'success': True, 'list_name': name, 'exists': setting is not None,
                'list_revision': setting['revision'] if setting else None,
                'items': all_items if include_inactive else planned, 'currency': 'CNY',
                'total_budget': budget, 'planned_allocated_budget': format(allocated, '.2f'),
                'unallocated_budget': format(remaining, '.2f') if remaining is not None else None,
                'over_budget': remaining < 0 if remaining is not None else None,
                'items_without_budget': missing, 'budget_plan_complete': not missing,
                'actual_spend': None, 'current_prices_verified': False,
                'storage_scope': 'personal_Hermes_instance_not_chat_isolated',
                'note': '分配额=计划单件预算×数量，仅计planned项；未设预算项不按0元商品处理。剩余是计划额度，不是账户余额或实际省钱。此清单由本Hermes实例共享，非多用户隔离。'}


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def tool_descriptors():
    short = {'type': 'string', 'maxLength': 300}
    attrs = schema({**{k: short for k in ATTRS if k not in ('item_kind', 'quantity')},
                    'item_kind': {'type': 'string', 'enum': list(KINDS)},
                    'quantity': {'type': 'integer', 'minimum': 1, 'maximum': 100}})
    name = {'type': 'string', 'minLength': 1, 'maxLength': 80, 'default': '默认'}
    revision = {'type': 'integer', 'minimum': 1, 'description': '从shopping_get_list读取的当前版本；更新已有数据时必填，冲突先重新读取。'}
    item = schema({**{k: short for k in ('title', 'product_query', 'spec', 'use_case')},
                   'preferences': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'maxLength': 200}},
                   'quantity': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                   'unit_budget': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1000000,
                                   'description': '用户计划的单件预算，CNY，最多两位小数；null清除预算，不是报价。'},
                   'status': {'type': 'string', 'enum': list(STATUSES)},
                   'urls': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'},
                            'description': '完整官方京东/淘宝/天猫商品链接，可为空。'}})
    return [
        {'name': 'shopping_check_variants',
         'description': '同款/规格核对：比较提供的品牌、型号、规格、成色、整机/配件/套装、数量，列出冲突、缺项与标题风险。仅核对调用方字段，不独立验证同SKU，不自动猜缺失规格。',
         'inputSchema': schema({'target': attrs, 'candidates': {'type': 'array', 'minItems': 1, 'maxItems': 8,
             'items': schema({'label': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                              'title': {'type': 'string', 'maxLength': 500}, 'url': {'type': 'string'},
                              'attributes': attrs}, ['label', 'attributes'])}}, ['target', 'candidates'])},
        {'name': 'shopping_review_digest',
         'description': '检索指定型号的公开评价证据，按舒适、可靠性、性能、续航、做工、售后主题保留原文片段和来源。标出待核对的相反措辞，不代表平台已购评论、评分或故障率；片段可能缺上下文。',
         'inputSchema': schema({'query': {'type': 'string', 'minLength': 1, 'maxLength': 120},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'default': 3},
             'review_sites': {'type': 'array', 'minItems': 1, 'maxItems': 2,
                             'items': {'type': 'string', 'enum': ['smzdm', 'zhihu', 'bilibili', 'v2ex']}}}, ['query'])},
        {'name': 'shopping_save_item',
         'description': '仅在用户要求记住/修改购物清单时持久保存。新增需item.title及product_query；更新需item_id及当前expected_revision，item只传要改的字段。支持用途、偏好、规格、数量、单件预算、planned/purchased/archived；这是本Hermes个人实例共享清单，不按聊天/多用户隔离。',
         'inputSchema': schema({'item': item, 'list_name': name, 'item_id': {'type': 'string', 'maxLength': 36},
                                'expected_revision': revision}, ['item'])},
        {'name': 'shopping_get_list',
         'description': '读取持久购物清单、用途、偏好、总预算及单件预算分配；标出缺预算与超预算，不把预算当报价或实际支出。返回修改所需item revision和list_revision；不查账号、不下单。',
         'inputSchema': schema({'list_name': name, 'include_inactive': {'type': 'boolean', 'default': False}})},
        {'name': 'shopping_set_budget',
         'description': '用户要求设置购物清单总预算时使用；已有清单需expected_revision=list_revision，从shopping_get_list获取。CNY预算最多两位小数；null清除总预算。不会触发付款或价格监控。',
         'inputSchema': schema({'list_name': name, 'total_budget': {'type': ['number', 'null'], 'minimum': 0,
                    'maximum': 1000000}, 'expected_revision': revision}, ['total_budget'])},
    ]
