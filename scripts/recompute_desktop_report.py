"""Recompute a full JSON recording without changing its historical evidence."""
import argparse
import hashlib
import json
from pathlib import Path

from adaptive_os.experiments import recompute_recording


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('recording', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = args.recording.read_bytes()
    report = recompute_recording(json.loads(data))
    report['source_sha256'] = hashlib.sha256(data).hexdigest()
    with args.output.open('x') as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write('\n')
    print(f'{len(report["results"])} trials recomputed to {args.output}')


if __name__ == '__main__':
    main()
