"""Build the standalone helper independently of Qt, with complete licence data."""
import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--licenses-only', action='store_true', help='Finish licence collection after a successful compiler run')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for line in (root / 'requirements-laya.lock').read_text().splitlines():
        if line and not line.startswith('#'):
            name, version = line.split('==')
            if importlib.metadata.version(name) != version:
                raise SystemExit(f'Build environment differs from lock: {name}')
    transformers = Path(importlib.util.find_spec('transformers').origin).parent
    excluded_models = ['transformers.models.' + path.name for path in (transformers / 'models').iterdir()
                       if path.is_dir() and path.name not in ('auto', 'bert', 'modernbert', 'encoder_decoder', '__pycache__')]
    command = [sys.executable, '-m', 'nuitka', '--mode=standalone', '--output-filename=model_helper',
        '--output-dir=' + str(root / 'build' / 'model-helper'), '--assume-yes-for-downloads', '--jobs=4',
        '--module-parameter=torch-disable-jit=yes',
        # Native extension compilation is disabled. Shipping 9,805 SDK headers
        # also exceeds macOS ARG_MAX in Nuitka 4.1.1's codesign command.
        '--noinclude-data-files=torch/include',
        '--include-module=laya.agent', '--include-package-data=laya', '--include-package-data=certifi',
        '--include-distribution-metadata=laya', '--include-distribution-metadata=transformers',
        '--include-distribution-metadata=torch', '--include-distribution-metadata=tokenizers',
        '--include-package=transformers.models.modernbert', '--include-package=transformers.models.bert',
        '--include-package=transformers.models.auto',
        '--include-package=transformers.models.encoder_decoder',
        '--nofollow-import-to=' + ','.join(['pytest', 'IPython', 'tkinter', *excluded_models]),
        str(root / 'model_helper.py')]
    if args.dry_run:
        print(json.dumps(command))
        return
    if not args.licenses_only:
        subprocess.run(command, cwd=root, check=True)
    destination = root / 'build' / 'model-helper' / 'model_helper.dist'
    licenses = destination / 'licenses'
    licenses.mkdir(exist_ok=True)
    python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata['Name']
        if name.lower() in ('adaptive-os-mvp', 'pip', 'setuptools'):
            continue
        target = licenses / name
        target.mkdir(exist_ok=True)
        (target / 'METADATA.txt').write_text(distribution.read_text('METADATA') or '')
        for file in distribution.files or ():
            if any(token in file.name.lower() for token in ('license', 'copying', 'notice')):
                source = distribution.locate_file(file)
                if source.is_file():
                    shutil.copy2(source, target / str(file).replace('/', '_'))
    shutil.copy2(root / 'requirements-laya.lock', destination / 'requirements-laya.lock')
    print(destination)


if __name__ == '__main__':
    main()
