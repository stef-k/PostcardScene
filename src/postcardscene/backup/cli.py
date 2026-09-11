"""One packaged create/verify/list/scheduled/restore CLI, with sanitized JSON output."""

import argparse
import json
from dataclasses import asdict

from . import create, list_backups, verify
from .files import BackupError
from .restore_execute import restore_archive
from .scheduled import scheduled


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("create").add_argument("--destination", required=True)
    commands.add_parser("verify").add_argument("archive")
    commands.add_parser("list").add_argument("--destination", required=True)
    commands.add_parser("scheduled")
    commands.add_parser("restore").add_argument("archive")
    args = parser.parse_args(argv)
    try:
        if args.operation == "create":
            result = asdict(create(args.destination))
        elif args.operation == "verify":
            result = asdict(verify(args.archive))
        elif args.operation == "restore":
            result = restore_archive(args.archive)
        elif args.operation == "scheduled":
            result = scheduled()
        else:
            result = [asdict(entry) for entry in list_backups(args.destination)]
        print(json.dumps(result, sort_keys=True))
        return 0
    except BackupError as error:
        category = str(error)
        if category not in {
            "restore_root_required",
            "restore_preparation_failed",
            "restore_candidate_invalid",
            "restore_failed_stopped",
            "restore_failed_rolled_back",
            "restore_manual_recovery_required",
            "restore_activation_failed",
            "restore_cleanup_uncertain",
            "operation_busy",
            "policy_unavailable",
            "timezone_unavailable",
        }:
            category = "backup_failed"
        result = {"error": category}
        if getattr(error, "cleanup_uncertain", False):
            result["cleanup"] = "restore_cleanup_uncertain"
        print(json.dumps(result))
        return 1
    except (Exception, KeyboardInterrupt):
        print('{"error":"backup_failed"}')
        return 1
