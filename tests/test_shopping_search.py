import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from shop_mcp import server as shopping


class ShoppingTests(unittest.TestCase):
    def test_link_keeps_id_and_removes_tracking(self):
        result = shopping.parse_product_url('https://item.taobao.com/item.htm?id=671399789358&spm=tracking')
        self.assertEqual(result['url'], 'https://item.taobao.com/item.htm?id=671399789358')
        self.assertEqual(shopping.parse_product_url('https://item.m.jd.com/product/100075593303.html')['product_id'], '100075593303')

    def test_reject_non_product_and_private_destinations(self):
        for url in ['http://item.jd.com/100075593303.html', 'https://item.jd.com.evil.test/100075593303.html',
                    'https://127.0.0.1/admin', 'https://item.jd.com:443/100075593303.html',
                    'https://user:pass@item.jd.com/100075593303.html', 'https://jd.com/orders',
                    'https://item.taobao.com/item.htm?id=12345&id=67890']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                shopping.parse_product_url(url)

    def test_filter_irrelevant_model_wrong_host_and_duplicate(self):
        raw = {'data': {'web': [
            {'title': '罗技 G304 无线鼠标', 'url': 'https://item.jd.com/100075593303.html', 'description': '￥99历史活动'},
            {'title': '罗技 G304 无线鼠标', 'url': 'https://item.jd.com/100075593303.html?tracking=x'},
            {'title': '罗技 G304X 升级款', 'url': 'https://item.jd.com/100075593304.html'},
            {'title': '罗技 G304', 'url': 'https://evil.test/product'},
            {'title': '机械键盘', 'url': 'https://item.jd.com/100075593305.html'}]}}
        rows, discarded = shopping.normalize_rows(raw, '罗技 G304', 'jd', 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(discarded, 3)
        self.assertIsNone(rows[0]['current_price'])
        self.assertFalse(rows[0]['current_price_verified'])

    def test_no_index_match_is_not_missing_stock(self):
        fake = AsyncMock(return_value={'success': True, 'data': {'web': []}})
        with patch.object(shopping, 'native', fake):
            result = asyncio.run(shopping.search_products('机械键盘', ['taobao'], 2))
        self.assertTrue(result['success'])
        self.assertEqual(result['platforms'][0]['status'], 'no_reliable_index_matches')
        self.assertFalse(result['platform_native_search'])
        self.assertEqual(result['candidate_count'], 0)
        self.assertIn('manual_search_links', result)

    def test_partial_provider_failure_is_visible(self):
        async def fake(action, target, limit):
            if 'item.taobao.com' in target:
                raise asyncio.TimeoutError()
            return {'success': True, 'data': {'web': [{'title': '机械键盘', 'url': 'https://item.jd.com/100075593303.html'}]}}
        with patch.object(shopping, 'native', fake):
            result = asyncio.run(shopping.search_products('机械键盘', ['jd', 'taobao'], 2))
        self.assertTrue(result['success'])
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['platforms'][1]['status'], 'provider_timeout')

    def test_failed_extraction_never_invents_details(self):
        fake = AsyncMock(return_value={'success': False, 'error': 'login needed'})
        with patch.object(shopping, 'native', fake):
            result = asyncio.run(shopping.read_product('https://item.jd.com/100075593303.html'))
        self.assertFalse(result['success'])
        self.assertEqual(result['pages'], [])
        self.assertIsNone(result['current_price'])


if __name__ == '__main__':
    unittest.main()
