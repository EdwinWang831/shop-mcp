"""Offline transport/schema/persistence acceptance; uses temporary databases only."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_server_and_cross_process_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'SHOPPING_DATA_DIR': directory,
                   'SHOPPING_WISHLIST_DB': str(Path(directory)/'wishlist.sqlite3'),
                   'SHOPPING_PRICE_DB': str(Path(directory)/'prices.sqlite3')}
            params = StdioServerParameters(command=sys.executable, args=['-m', 'shop_mcp'], env=env)
            saved_id = None
            for iteration in range(2):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        names = [t.name for t in (await session.list_tools()).tools]
                        self.assertEqual(len(names), 18)
                        async def call(name, args):
                            result = await session.call_tool(name, args)
                            self.assertFalse(result.isError, result)
                            return json.loads(result.content[0].text)
                        if iteration == 0:
                            saved = await call('shopping_save_item', {'item': {'title': '测试鼠标', 'product_query': 'G304', 'unit_budget': 200}})
                            saved_id = saved['item']['item_id']
                            q = {'url': 'https://item.jd.com/100075593303.html', 'model': 'G304', 'spec': '白色',
                                 'condition': '全新', 'quantity': 1, 'buyer_context': '普通账户', 'delivery_context': '同一区域',
                                 'currency': 'CNY', 'stage': 'estimate', 'observed_at': datetime.now(timezone.utc).isoformat(),
                                 'price_basis': 'before_discounts', 'item_subtotal': 199, 'shipping_total': 6,
                                 'discounts': [{'label': '店铺券', 'amount': 20, 'applied': True, 'eligibility': 'confirmed', 'kind': 'instant'}]}
                            calc = await call('shopping_calculate_checkout', {'quote': q})
                            self.assertEqual(calc['payable_total'], '185.00')
                            self.assertFalse(calc['current_price_verified'])
                            refused = await call('shopping_calculate_checkout', {'quote': {**q, 'shipping_total': None}})
                            self.assertFalse(refused['success'])
                        else:
                            value = await call('shopping_get_list', {})
                            self.assertEqual(value['items'][0]['item_id'], saved_id)
                            self.assertEqual(value['planned_allocated_budget'], '200.00')
            quiet = subprocess.run([sys.executable, '-m', 'shop_mcp.price_watch'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertEqual(quiet.stdout, '')
