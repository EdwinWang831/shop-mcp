"""Run shopping web retrieval in the existing Hermes provider runtime."""
import argparse
import os
import asyncio
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(os.environ.get('SHOPPING_HERMES_AGENT_ROOT', str(Path.home() / '.hermes/hermes-agent'))).expanduser()))
import hermes_bootstrap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['search', 'extract'])
    parser.add_argument('target')
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    # Some existing plugins print initialization notices. Keep MCP JSON clean.
    with contextlib.redirect_stdout(sys.stderr):
        if args.action == 'search':
            from plugins.web.keyless_mcp import exa_search_keyless
            result = exa_search_keyless(args.target, args.limit)
        else:
            from tools.web_tools import web_extract_tool
            raw = json.loads(asyncio.run(web_extract_tool([args.target], char_limit=10000)))
            if isinstance(raw, dict) and isinstance(raw.get('results'), list):
                pages = raw['results']
                result = {'success': any(p.get('content') and not p.get('error') for p in pages if isinstance(p, dict)),
                          'data': {'results': pages}}
            else:
                result = raw
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'success': False, 'error': 'PROVIDER_REQUEST_FAILED'}))
        sys.exit(1)
