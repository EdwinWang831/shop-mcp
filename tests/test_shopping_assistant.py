import json
from pathlib import Path
import tempfile
import unittest

from shop_mcp.assistant import Wishlist, check_variants, digest_evidence
from shop_mcp.server import parse_product_url


class ShoppingAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'data/wishlist.sqlite3'
        self.store = Wishlist(self.path, parse_product_url)

    def tearDown(self):
        self.temp.cleanup()

    def attrs(self):
        return dict(brand='罗技', model='G304', spec='白色标准版', condition='全新', item_kind='full_product', quantity=1)

    def check(self, attrs=None, title='罗技 G304 鼠标'):
        return check_variants(self.attrs(), [{'label': '测试', 'title': title, 'attributes': self.attrs() if attrs is None else attrs}], parse_product_url)['candidates'][0]

    def test_conflicting_model_spec_condition_quantity_and_accessory(self):
        for field, value in [('model', 'G304X'), ('spec', '黑色'), ('condition', '二手'), ('quantity', 2), ('item_kind', 'accessory')]:
            with self.subTest(field=field):
                result = self.check({**self.attrs(), field: value})
                self.assertEqual(result['status'], 'different_declared_attributes')
                self.assertEqual(result['conflicts'][0]['field'], field)

    def test_missing_specs_and_unknowns_not_same_sku(self):
        r = self.check({'brand': '罗技', 'model': 'G304'})
        self.assertEqual(r['status'], 'insufficient_information')
        self.assertIn('spec', r['missing_fields'])
        self.assertEqual(self.check({**self.attrs(), 'spec': '未知'})['status'], 'insufficient_information')

    def test_title_accessory_and_different_model_override_declared_match(self):
        for title in ['罗技 G304 适用脚贴', '罗技 G304X 鼠标', '罗技 G502 鼠标']:
            with self.subTest(title=title):
                self.assertEqual(self.check(title=title)['status'], 'insufficient_information')
        matched = self.check()
        self.assertEqual(matched['status'], 'matching_declared_attributes')
        self.assertFalse(matched['sku_equivalence_verified'])

    def save(self, **updates):
        return self.store.save({'title': '鼠标', 'product_query': '罗技 G304', 'use_case': '办公', 'preferences': ['无线'], **updates})['item']

    def test_save_restart_and_partial_update_preserves_preferences(self):
        item = self.save(unit_budget=180)
        reopened = Wishlist(self.path, parse_product_url)
        saved = reopened.get()['items'][0]
        self.assertEqual(saved['item_id'], item['item_id'])
        updated = reopened.save({'unit_budget': 200}, item_id=item['item_id'], expected_revision=1)['item']
        self.assertEqual(updated['preferences'], ['无线'])
        self.assertEqual(updated['unit_budget'], '200.00')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_stale_revision_cannot_overwrite_item_or_budget(self):
        item = self.save()
        self.store.save({'spec': '白色'}, item_id=item['item_id'], expected_revision=1)
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            self.store.save({'spec': '黑色'}, item_id=item['item_id'], expected_revision=1)
        self.store.set_budget(1000, expected_revision=1)
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            self.store.set_budget(300, expected_revision=1)
        self.assertEqual(self.store.get()['total_budget'], '1000.00')

    def test_budget_is_plan_not_spend_and_missing_budget_explicit(self):
        self.store.set_budget(300)
        item = self.save(unit_budget=180, quantity=2)
        self.store.save({'title': '键盘', 'product_query': '键盘'})
        r = self.store.get()
        self.assertEqual(r['planned_allocated_budget'], '360.00')
        self.assertEqual(r['unallocated_budget'], '-60.00')
        self.assertTrue(r['over_budget'])
        self.assertFalse(r['budget_plan_complete'])
        self.assertEqual(len(r['items_without_budget']), 1)
        self.assertIsNone(r['actual_spend'])
        self.store.save({'status': 'purchased'}, item_id=item['item_id'], expected_revision=1)
        self.assertEqual(self.store.get()['planned_allocated_budget'], '0.00')
        self.assertEqual(len(self.store.get(include_inactive=True)['items']), 2)

    def test_duplicate_title_and_unsafe_url_do_not_add_items(self):
        self.save()
        r = self.store.save({'title': '鼠标', 'product_query': '罗技 G304'})
        self.assertFalse(r['success'])
        with self.assertRaises(ValueError):
            self.store.save({'title': '坏链接', 'product_query': '鼠标', 'urls': ['https://127.0.0.1/item.htm?id=12345']})
        self.assertEqual(len(self.store.get()['items']), 1)

    def test_decimal_validation_prevents_invalid_mutations(self):
        item = self.save(unit_budget=100)
        for invalid in [float('nan'), float('inf'), True, -1, 100.001]:
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.store.save({'unit_budget': invalid}, item_id=item['item_id'], expected_revision=1)
        self.assertEqual(self.store.get()['items'][0]['unit_budget'], '100.00')

    def test_named_lists_and_archive_preserve_history(self):
        name = "办公'; DROP TABLE items; --"
        item = self.store.save({'title': '鼠标', 'product_query': 'G304'}, list_name=name)['item']
        self.assertEqual(self.store.get()['items'], [])
        self.store.save({'status': 'archived'}, list_name=name, item_id=item['item_id'], expected_revision=1)
        self.assertEqual(self.store.get(list_name=name)['items'], [])
        self.assertEqual(len(self.store.get(list_name=name, include_inactive=True)['items']), 1)

    def source(self, url, snippet):
        return {'url': url, 'title': 'G304体验', 'snippet': snippet, 'source_published_at': None,
                'full_article_read': False, 'purchase_verified': False}

    def test_review_opposition_retains_verbatim_sources_without_semantic_claims(self):
        evidence = {'product_query': 'G304', 'public_reviews': [self.source('https://post.smzdm.com/p/one/', '握持舒服。'),
                 self.source('https://post.smzdm.com/p/two/', '握持不舒服，手感硌手。')], 'channels': []}
        r = digest_evidence(evidence)
        comfort = next(t for t in r['topics'] if t['topic'] == '舒适与尺寸')
        self.assertTrue(comfort['possible_opposing_wording'])
        self.assertFalse(comfort['conflict_verified'])
        negative = comfort['statements'][1]
        self.assertEqual(negative['excerpt'], '握持不舒服，手感硌手')
        self.assertEqual(negative['cue_words']['positive_words'], [])
        self.assertIsNone(negative['source_published_at'])
        self.assertFalse(r['platform_buyer_reviews_available'])

    def test_no_evidence_and_unclassified_source_remain_explicit(self):
        r = digest_evidence({'product_query': 'G304', 'public_reviews': [], 'channels': [{'status': 'provider_timeout'}]})
        self.assertFalse(r['success'])
        self.assertEqual(r['topics'], [])
        r = digest_evidence({'product_query': 'G304', 'public_reviews': [self.source('https://post.smzdm.com/p/one/', '我是普通用户。')], 'channels': []})
        self.assertEqual(r['unclassified_sources'], ['https://post.smzdm.com/p/one/'])


if __name__ == '__main__':
    unittest.main()
