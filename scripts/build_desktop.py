"""Build a local macOS .app with Qt's deployment tool; no system installation."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from adaptive_os.sandbox_runtime import CONTEXT_FILES, build_context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('This packaging target is macOS only; development launch is adaptive-os desktop.')
    root = Path(__file__).resolve().parents[1]
    (root / 'build').mkdir(exist_ok=True)
    (root / 'dist').mkdir(exist_ok=True)
    deploy = Path(sys.executable).parent / 'pyside6-deploy'
    if not deploy.is_file():
        raise SystemExit('Install desktop dependencies first: python -m pip install -e ".[desktop]"')
    destination = root / 'dist' / 'Adaptive OS Controller.app'
    if destination.exists() and not args.dry_run:
        backup = root / 'dist' / ('previous-' + uuid.uuid4().hex[:8] + '.app')
        destination.rename(backup)
    # Deploy updates its configuration; keep the checked-in template portable.
    with tempfile.TemporaryDirectory(prefix='adaptive-os-build-', dir=root / 'build') as temporary:
        config = Path(temporary) / 'pysidedeploy.spec'
        shutil.copy2(root / 'pysidedeploy.spec', config)
        command = [str(deploy), str(root / 'desktop_app.py'), '-c', str(config), '-f',
                   '--nuitka-version', '4.1.1',
                   '--extra-ignore-dirs', 'tests,build,dist,runs,.superpowers,.codebase-memory']
        if args.dry_run:
            command.append('--dry-run')
        subprocess.run(command, cwd=root, check=True)
    if args.dry_run:
        return
    if not destination.is_dir():
        raise SystemExit('Qt deployment did not produce dist/Adaptive OS Controller.app; inspect the build output.')
    resources = destination / 'Contents' / 'Resources' / 'sandbox'
    resources.mkdir(parents=True, exist_ok=True)
    for name in CONTEXT_FILES:
        shutil.copy2(build_context() / name, resources / name)
    # Ad-hoc local signing seals the resources we just copied; not notarization.
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(destination)], check=True)
    print(destination)


if __name__ == '__main__':
    main()
