import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from shop_mcp.commerce import PriceStore, calculate_checkout, compare_sellers
from shop_mcp.server import parse_product_url


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.store = PriceStore(self.path, parse_product_url)

    def tearDown(self):
        self.temp.cleanup()

    def quote(self, **overrides):
        return {'model': 'G304', 'spec': '白色标准版', 'condition': '全新', 'quantity': 1,
                'buyer_context': '普通账户', 'delivery_context': '同一区域', 'currency': 'CNY',
                'url': 'https://item.jd.com/100075593303.html', 'observed_at': datetime.now(timezone.utc).isoformat(),
                'stage': 'checkout_preview', 'price_basis': 'before_discounts', 'item_subtotal': 199,
                'shipping_total': 6, 'discounts': [], 'payable_total': 185, **overrides}

    def discount(self, **overrides):
        return {'label': '店铺券', 'amount': 20, 'applied': True, 'eligibility': 'confirmed', 'kind': 'instant', **overrides}

    def test_checkout_arithmetic_excludes_pending_coupon_and_rebate(self):
        q = self.quote(discounts=[self.discount(), self.discount(label='会员券', applied=False, eligibility='unknown', amount=30),
                                  self.discount(label='返现', kind='post_purchase_rebate', amount=5)])
        r = calculate_checkout(q, parse_product_url)
        self.assertEqual(r['payable_total'], '185.00')
        self.assertEqual(len(r['excluded_discounts']), 2)
        self.assertFalse(r['independently_fetched'])

    def test_missing_shipping_eligibility_and_unknown_context_block_comparison(self):
        self.assertFalse(calculate_checkout(self.quote(shipping_total=None), parse_product_url)['success'])
        with self.assertRaisesRegex(ValueError, 'ELIGIBILITY'):
            calculate_checkout(self.quote(discounts=[self.discount(eligibility='unknown')]), parse_product_url)
        with self.assertRaisesRegex(ValueError, 'CONTEXT'):
            calculate_checkout(self.quote(buyer_context='未知'), parse_product_url)

    def test_double_deduction_mutual_exclusion_and_preview_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'DOUBLE_DISCOUNT'):
            calculate_checkout(self.quote(price_basis='already_discounted', discounts=[self.discount()]), parse_product_url)
        with self.assertRaisesRegex(ValueError, 'MUTUALLY'):
            calculate_checkout(self.quote(discounts=[self.discount(exclusive_group='one'), self.discount(label='另一券', exclusive_group='one')]), parse_product_url)
        r = calculate_checkout(self.quote(checkout_preview_total=100), parse_product_url)
        self.assertEqual(r['error'], 'PREVIEW_TOTAL_MISMATCH')

    def test_quantity_does_not_multiply_already_total_subtotal(self):
        r = calculate_checkout(self.quote(quantity=2, item_subtotal=398, shipping_total=0), parse_product_url)
        self.assertEqual(r['payable_total'], '398.00')

    def test_history_groups_incompatible_specs_and_keeps_actual_observation_dates(self):
        old = self.quote(observed_at=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat(), payable_total=199)
        self.store.record('罗技 G304', old)
        self.store.record('罗技 G304', self.quote(payable_total=180))
        self.store.record('罗技 G304', self.quote(spec='黑色', payable_total=160))
        restored = PriceStore(self.path, parse_product_url)
        r = restored.history('罗技 G304')
        self.assertEqual(len(r['checkout_history_groups']), 2)
        white = next(g for g in r['checkout_history_groups'] if g['context']['spec'] == '白色标准版')
        self.assertEqual(white['lowest_recorded_total'], '180.00')
        self.assertEqual(len(white['observations']), 2)
        self.assertFalse(r['complete_market_history'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_fresh_checkout_alert_matches_context_and_is_deduplicated(self):
        q = self.quote(payable_total=150)
        self.store.watch('create', query='罗技 G304', target_amount=160, mode='checkout_quotes', quote_context=q)
        wrong = self.store.record('罗技 G304', {**q, 'spec': '黑色'})
        self.assertEqual(wrong['new_alerts'], [])
        first = self.store.record('罗技 G304', q)
        self.assertEqual(len(first['new_alerts']), 1)
        self.assertEqual(self.store.record('罗技 G304', q)['new_alerts'], [])
        restored = PriceStore(self.path, parse_product_url)
        aid = restored.alerts()['alerts'][0]['alert_id']
        self.assertEqual(restored.alerts(acknowledge_ids=[aid])['alerts'], [])

    def test_old_checkout_estimates_and_invalid_dates_cannot_trigger_alerts(self):
        q = self.quote(payable_total=99)
        self.store.watch('create', query='G304', target_amount=200, mode='checkout_quotes', quote_context=q)
        old = {**q, 'observed_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
        self.assertEqual(self.store.record('G304', old)['new_alerts'], [])
        for update in [{'stage': 'estimate'}, {'observed_at': '2026-01-01'}, {'payable_total': float('nan')}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.record('G304', {**q, **update})

    def source(self, title='罗技 G304 无线鼠标 149元', url='https://www.smzdm.com/p/182854208/'):
        return {'title': title, 'url': url}

    def test_public_amount_is_reference_and_ambiguous_titles_do_not_trigger(self):
        for title in ['罗技 G304 149元 199元', '罗技 G304 适用脚贴 9元', '罗技 G304 优惠券20元', 'G304和G102鼠标149元', '罗技 G304 黑色鼠标 省36.68元', 'G304 鼠标 立减20元', 'G304 鼠标 返5元']:
            with self.subTest(title=title):
                self.assertIsNone(self.store.reference_candidates('G304', [self.source(title)])[0]['amount'])
        self.assertEqual(self.store.reference_candidates('G304', [self.source('罗技 G304X 149元')]), [])
        self.assertEqual(self.store.reference_candidates('G304', [self.source(url='https://www.smzdm.com/ju/sjlrz3v/')]), [])
        r = self.store.reference_candidates('G304', [self.source()])[0]
        self.assertEqual(r['amount'], '149.00')
        self.assertIsNone(r['source_published_at'])
        self.assertFalse(r['current_price_verified'])

    def test_public_watch_runs_real_adapter_contract_and_deduplicates_across_restart(self):
        w = self.store.watch('create', query='G304', mode='public_reference', target_amount=150)
        fake = AsyncMock(return_value={'product_query': 'G304', 'price_references': [self.source()], 'channels': [{'channel': 'prices', 'status': 'ok'}]})
        r = asyncio.run(self.store.check_watches(None, fake))
        self.assertEqual(len(r['new_alerts']), 1)
        self.assertIn('未核实', r['new_alerts'][0]['note'])
        restored = PriceStore(self.path, parse_product_url)
        r = asyncio.run(restored.check_watches(None, fake))
        self.assertEqual(r['new_alerts'], [])
        history = restored.history('G304')
        self.assertEqual(history['checkout_history_groups'], [])
        self.assertEqual(len(history['public_reference_observations']), 1)
        paused = restored.watch('pause', watch_id=w['watch_id'], expected_revision=1)
        self.assertFalse(paused['enabled'])
        with self.assertRaisesRegex(ValueError, 'REVISION'):
            restored.watch('resume', watch_id=w['watch_id'], expected_revision=1)

    def test_seller_text_and_official_general_rules_have_separate_provenance(self):
        fetch = AsyncMock(return_value={'success': True, 'data': {'web': [
            {'title': '京东退换货规则', 'url': 'https://help.jd.com/user/issue/123.html', 'description': '七天无理由规则'},
            {'title': '冒充官方', 'url': 'https://help.jd.com.evil.test/a', 'description': '全部免费'}]}})
        reader = AsyncMock(return_value={'status': 'limited_or_unavailable_page', 'pages': []})
        offers = [{'label': 'A', 'url': self.quote()['url'], 'provided_policy_text': '店铺写明7天无理由退货，退货运费买家承担。保修1年，可开电子发票。'},
                  {'label': 'B', 'url': 'https://item.jd.com/100012345678.html'}]
        r = asyncio.run(compare_sellers(offers, fetch, reader, parse_product_url))
        self.assertTrue(r['offers'][0]['facet_excerpts']['保修'])
        self.assertIn('保修', r['offers'][1]['missing_facets'])
        self.assertFalse(r['offers'][0]['listing_specific_verified'])
        self.assertEqual(len(r['official_general_policy_references'][0]['sources']), 1)
        self.assertIsNone(r['winner'])


if __name__ == '__main__':
    unittest.main()
