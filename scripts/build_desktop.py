"""Build a local macOS .app with Qt's deployment tool; no system installation."""

import argparse
import configparser
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
    parser.add_argument('--output-dir', type=Path, help='Stage a bundle separately from the running app')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('This packaging target is macOS only; development launch is adaptive-os desktop.')
    root = Path(__file__).resolve().parents[1]
    (root / 'build').mkdir(exist_ok=True)
    output = args.output_dir.resolve() if args.output_dir else root / 'dist'
    output.mkdir(parents=True, exist_ok=True)
    deploy = Path(sys.executable).parent / 'pyside6-deploy'
    if not deploy.is_file():
        raise SystemExit('Install desktop dependencies first: python -m pip install -e ".[desktop]"')
    helper = root / 'build' / 'model-helper' / 'model_helper.dist'
    checkpoint = root / 'build' / 'laya-checkpoint'
    if not args.dry_run:
        from adaptive_os.model_runtime import verify_checkpoint, LAYA_REVISION
        if not (helper / 'model_helper').is_file():
            raise SystemExit('Build the standalone helper first: build/laya-venv/bin/python scripts/build_model_helper.py')
        if verify_checkpoint(checkpoint)['revision'] != LAYA_REVISION:
            raise SystemExit('Prepare the pinned Laya checkpoint before bundling.')
    destination = output / 'Adaptive OS Controller.app'
    if destination.exists() and not args.dry_run:
        backup = output / ('previous-' + uuid.uuid4().hex[:8] + '.app')
        destination.rename(backup)
    # Deploy updates its configuration; keep the checked-in template portable.
    with tempfile.TemporaryDirectory(prefix='adaptive-os-build-', dir=root / 'build') as temporary:
        config = Path(temporary) / 'pysidedeploy.spec'
        shutil.copy2(root / 'pysidedeploy.spec', config)
        settings = configparser.ConfigParser()
        settings.read(config)
        settings['app']['exec_directory'] = str(output)
        with config.open('w') as stream:
            settings.write(stream)
        command = [str(deploy), str(root / 'desktop_app.py'), '-c', str(config), '-f',
                   '--nuitka-version', '4.1.1',
                   '--extra-ignore-dirs', 'tests,build,dist,runs,.superpowers,.codebase-memory']
        if args.dry_run:
            command.append('--dry-run')
        subprocess.run(command, cwd=root, check=True)
    if args.dry_run:
        return
    if not destination.is_dir():
        raise SystemExit(f'Qt deployment did not produce {destination}; inspect the build output.')
    resources = destination / 'Contents' / 'Resources' / 'sandbox'
    resources.mkdir(parents=True, exist_ok=True)
    for name in CONTEXT_FILES:
        shutil.copy2(build_context() / name, resources / name)
    shutil.copytree(helper, destination / 'Contents' / 'Helpers' / 'model_helper', dirs_exist_ok=True)
    shutil.copytree(checkpoint, destination / 'Contents' / 'Resources' / 'laya', dirs_exist_ok=True)
    # The pinned model card declares Apache-2.0, as does its runtime. Include the
    # full licence next to the weights as well as in the dependency inventory.
    model_license = helper / 'licenses' / 'laya' / 'laya-0.3.27.dist-info_licenses_LICENSE'
    shutil.copy2(model_license, destination / 'Contents' / 'Resources' / 'laya' / 'LICENSE.txt')
    shutil.copy2(root / 'requirements-laya.lock', destination / 'Contents' / 'Resources' / 'requirements-laya.lock')
    # Ad-hoc local signing seals the resources we just copied; not notarization.
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(destination)], check=True)
    print(destination)


if __name__ == '__main__':
    main()
