"""Print config or cron shim; never edit a user's Hermes configuration."""
import argparse
import json
from pathlib import Path
import shlex

parser = argparse.ArgumentParser()
parser.add_argument('--cron-script', action='store_true')
args = parser.parse_args()
repo = Path(__file__).resolve().parent.parent
if args.cron_script:
    print('#!/usr/bin/env bash\nset -euo pipefail\nexec ' + shlex.quote(str(repo/'.venv/bin/shop-mcp-price-watch')))
else:
    print('mcp_servers:\n  shopping_search:\n    command: ' + json.dumps(str(repo/'.venv/bin/shop-mcp')) + '\n    args: []\n    timeout: 100')
