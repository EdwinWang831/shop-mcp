"""No-agent cron entry point; empty stdout means no new notification."""
import asyncio
import json
import os
import sys

from .server import DATA_DIR, native, research_product, parse_product_url
from .commerce import PriceStore


async def main():
    store = PriceStore(os.environ.get('SHOPPING_PRICE_DB', str(DATA_DIR / 'prices.sqlite3')), parse_product_url)
    result = await store.check_watches(native, research_product)
    failed = [c for c in result['checks'] if c['status'] in ('provider_error', 'provider_timeout')]
    for event in result['new_alerts']:
        title = '公开优惠线索提醒（现价未核实）' if event['kind'] == 'public_reference' else '提供的结算价达到目标（未独立核验）'
        print(title + '\n' + event['query'] + '：' + event['amount'] + '元；目标≤' + event['threshold'] + '元\n' + event['url'] + '\n' + event['note'] + '\n')
    if failed:
        print(json.dumps({'error': 'PRICE_REFERENCE_CHECK_FAILED', 'failed_checks': failed}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def cli():
    sys.exit(asyncio.run(main()))


if __name__ == '__main__':
    cli()
