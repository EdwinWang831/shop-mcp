import asyncio
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import AsyncMock
from shop_mcp import evidence
from shop_mcp.server import parse_product_url


class EvidenceTests(unittest.TestCase):
    def test_reference_amount_keeps_context_without_currency_or_current_price_claim(self):
        result = evidence.amount_mentions('2023年我花了￥199买的，满200减20元优惠券。今天价格未知。')
        self.assertEqual([r['amount_text'] for r in result], ['199', '20'])
        self.assertIn('2023', result[0]['context'])
        self.assertFalse(result[0]['current_price_verified'])
        self.assertIsNone(result[0]['currency'])

    def test_reject_wrong_model_even_when_brand_is_shared(self):
        self.assertFalse(evidence.model_matches('罗技G304', '罗技 G304X新款'))
        self.assertFalse(evidence.model_matches('罗技 G304', '罗技 G502 HERO'))
        self.assertTrue(evidence.model_matches('罗技G304', '罗技G304无线鼠标'))

    def test_multi_product_review_does_not_transfer_keyboard_failure_to_mouse(self):
        text, scope = evidence.scoped_snippet('罗技 G102', '鼠标键盘选购综合帖',
            'G102是入门有线鼠标 ... G613键盘双击退货 ... G304使用电池')
        self.assertIn('G102', text)
        self.assertNotIn('G613', text)
        self.assertNotIn('双击', text)
        self.assertEqual(scope, 'explicit_target_model_segments_only')

    def test_review_source_is_not_purchase_verified_or_a_rating(self):
        fake = AsyncMock(return_value={'success': True, 'data': {'web': [
            {'title': '罗技 G304 优缺点', 'url': 'https://post.smzdm.com/p/example/', 'description': '用了一年，偶尔双击'},
            {'title': '罗技 G304X', 'url': 'https://post.smzdm.com/p/wrong-model/', 'description': '很轻'},
            {'title': '罗技 G304', 'url': 'https://post.smzdm.com.evil.test/p/evil/', 'description': '五星好评'}]}})
        r = asyncio.run(evidence.research_product('罗技G304', fake, review_sites=['smzdm']))
        self.assertEqual(len(r['public_reviews']), 1)
        self.assertFalse(r['public_reviews'][0]['purchase_verified'])
        self.assertFalse(r['public_reviews'][0]['full_article_read'])
        self.assertFalse(r['platform_buyer_reviews_available'])
        self.assertIsNone(r['platform_rating'])

    def test_mismatched_reference_price_cannot_choose_cheapest_product(self):
        fake = AsyncMock(return_value={'success': True, 'data': {'web': [
            {'title': 'G304和G102 使用比较', 'url': 'https://post.smzdm.com/p/example/', 'description': 'G304历史价199元，G102优惠券10元'}]}})
        r = asyncio.run(evidence.compare_products(['罗技 G304', '罗技 G102'], fake, review_sites=['smzdm']))
        self.assertTrue(r['success'])
        self.assertFalse(r['current_price_comparable'])
        self.assertIsNone(r['lowest_price_winner'])

    def quote(self):
        return {'url':'https://item.jd.com/100075593303.html','model':'G304','spec':'白色标准版',
                'condition':'全新','quantity':1,'buyer_context':'普通账户','delivery_context':'同一配送地区',
                'currency':'CNY','stage':'checkout_preview','payable_total':199,
                'observed_at':datetime.now(timezone.utc).isoformat()}

    def test_quote_calculation_is_only_caller_provided(self):
        q=self.quote(); r={**q,'url':'https://item.taobao.com/item.htm?id=671399789358','payable_total':189}
        value=evidence.compare_quotes([q,r],parse_product_url)
        self.assertEqual(value['saving_vs_highest'],'10')
        self.assertFalse(value['independently_fetched'])

    def test_non_equivalent_quotes_refused(self):
        q=self.quote()
        for update in [{'spec':'黑色升级版'},{'quantity':2},{'buyer_context':'PLUS会员'},
                       {'condition':'翻新'},{'delivery_context':'不同配送地区'},{'model':'G304X'},
                       {'stage':'listed_price'},{'payable_total':float('nan')},
                       {'observed_at':(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()}]:
            with self.subTest(update=update):
                r=evidence.compare_quotes([q,{**q,**update}],parse_product_url)
                self.assertFalse(r['success'])
                self.assertEqual(r['lowest_quotes'],[])

    def test_timeout_and_missing_evidence_are_explicit(self):
        fake=AsyncMock(side_effect=asyncio.TimeoutError())
        r=asyncio.run(evidence.research_product('罗技 G304',fake,review_sites=['smzdm']))
        self.assertFalse(r['success'])
        self.assertTrue(all(c['status']=='provider_timeout' for c in r['channels']))
        self.assertIsNone(r['current_price'])


if __name__ == '__main__':
    unittest.main()
