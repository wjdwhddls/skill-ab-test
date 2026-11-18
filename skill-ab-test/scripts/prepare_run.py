#!/usr/bin/env python3
"""Seal the common tasks and target packet, or capture submitted answer bytes."""
import argparse
import json
import sys
from pathlib import Path
from report_lib import ValidationError, prepare_run, capture_answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--capture', action='store_true', help='capture execution and answer hashes after both workers finish')
    args = parser.parse_args()
    try:
        result = capture_answers(args.run_dir) if args.capture else prepare_run(args.run_dir)
    except ValidationError as exc:
        print(f'검증 실패: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'status': 'captured' if args.capture else 'prepared', 'files': list(result['hashes'])}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
