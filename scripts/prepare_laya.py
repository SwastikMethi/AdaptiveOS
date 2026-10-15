"""Build-time only: fetch the English checkpoint against reviewed pinned digests."""
import hashlib
import json
from pathlib import Path
import subprocess

REVISION = '7b928d828b7b0e022f929d9bd2e44165aa270148'
def prepare(destination, manifest):
    for name, expected in manifest['files'].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        candidate = target
        if not target.exists():
            url = f'https://huggingface.co/convaiinnovations/laya/resolve/{manifest["revision"]}/{name}'
            candidate = target.with_suffix(target.suffix + '.partial')
            subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                            url, '--output', str(candidate)], check=True)
        with candidate.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError(f'Pinned checkpoint digest mismatch: {name}')
        if candidate != target:
            candidate.rename(target)
        print(name, target.stat().st_size, flush=True)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'laya-checkpoint.json').read_text())
    if manifest['revision'] != REVISION or manifest['repository'] != 'convaiinnovations/laya':
        raise ValueError('Checkpoint manifest does not match the pinned repository/revision')
    prepare(root / 'build' / 'laya-checkpoint', manifest)


if __name__ == '__main__':
    main()
