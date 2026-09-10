"""One packaged manual create/verify/list CLI, with sanitized JSON output."""

import argparse
import json
from dataclasses import asdict

from . import create, list_backups, verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("create").add_argument("--destination", required=True)
    commands.add_parser("verify").add_argument("archive")
    commands.add_parser("list").add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "create":
            result = asdict(create(args.destination))
        elif args.operation == "verify":
            result = asdict(verify(args.archive))
        else:
            result = [asdict(entry) for entry in list_backups(args.destination)]
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"error":"backup_failed"}')
        return 1
