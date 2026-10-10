"""Build a source + Windows dependency release using an explicit allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TOP = [
    'README.md', 'DEPLOY-WINDOWS.md', 'COCKPIT.md', 'COCKPIT-OUTBOUND.md',
    'MIGRATION-2026-10-09.md', '渠道进展_2026-10-07.md', 'STATUS.md',
    'REVIEW.md', 'PROGRESS.md', '使用焚决.md', '证实结果_2026-10-07.md',
    '站长接入与机制研究_2026-10-07.md', '协议验收摘要_2026-10-07.json',
    '协议研究来源_2026-10-07.json', 'SOURCE_PROVENANCE.json',
    'pyproject.toml', 'requirements.lock', 'local.example.json',
    '.gitignore', 'Install.cmd', '焚决.cmd', 'Start-Guard.cmd',
    'Stop-Guard.cmd', 'Status-Guard.cmd', 'Restore-Cockpit.cmd',
    'Start-Cockpit.cmd', 'Stop-Cockpit.cmd', 'Start-Workbench.cmd',
    'Stop-Workbench.cmd', 'Sub2API-Status.cmd',
]
REQUIRED = {
    'COCKPIT.md', 'COCKPIT-OUTBOUND.md', 'Install.cmd', '焚决.cmd',
    'Start-Guard.cmd', 'Stop-Guard.cmd', 'Status-Guard.cmd',
    'Restore-Cockpit.cmd', 'scripts/Start-Guard.ps1',
    'scripts/Stop-Guard.ps1', 'scripts/Status-Guard.ps1',
    'scripts/Install-CockpitPatch.ps1', 'cockpit-patch/fenjue_outbound.go',
    'cockpit-patch/fenjue_boot_windows.go',
    'cockpit-patch/fenjue_outbound_test.go', 'tests/test_cockpit_ui.cjs',
}
PROGRAM_SUFFIXES = {'.py', '.ps1', '.cmd', '.json', '.cjs'}


def entries(wheelhouse):
    wheelhouse = Path(wheelhouse).resolve()
    result = {name: ROOT / name for name in TOP}
    # Keep the old file unchanged in the checkout, but label it historical in a release.
    result['historical-release-validation.json'] = ROOT / 'RELEASE-VALIDATION.json'
    for folder, suffixes in [
        ('fenjue', {'.py', '.html'}), ('tests', {'.py', '.cjs'}),
        ('scripts', {'.py', '.ps1'}), ('protocol', {'.go'}),
        ('cockpit-patch', {'.go'}),
    ]:
        directory = ROOT / folder
        if not directory.is_dir():
            raise ValueError('Release source directory missing: ' + folder)
        for path in directory.rglob('*'):
            if path.is_file() and path.suffix.lower() in suffixes and '__pycache__' not in path.parts:
                result[path.relative_to(ROOT).as_posix()] = path
    wheels = list(wheelhouse.glob('*.whl'))
    if not wheels:
        raise ValueError('Wheelhouse is empty')
    result.update({'wheelhouse/' + p.name: p for p in wheels})
    missing = sorted(REQUIRED - result.keys())
    if missing:
        raise ValueError('Required release input missing: ' + ', '.join(missing))
    for name, path in result.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError('Release input missing or symlink: ' + name)
    return result


def git_status():
    return subprocess.check_output(
        ['git', 'status', '--porcelain=v1', '--untracked-files=all'],
        cwd=ROOT, text=True,
    )


def tree_hash(content):
    """Digest the exact packaged inputs, including wheels, before metadata."""
    digest = hashlib.sha256()
    for name in sorted(content):
        digest.update(name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(content[name])
        digest.update(b'\0')
    return digest.hexdigest()


def machine_path_in_program(name, raw):
    if Path(name).suffix.lower() not in PROGRAM_SUFFIXES:
        return False
    home = str(Path.home()).lower()
    # Accept neither slash spelling nor JSON/Python-escaped Windows paths.
    prefixes = {home.replace('\\', '/'), home.replace('/', '\\')}
    prefixes.update(prefix.replace('\\', '\\\\') for prefix in list(prefixes))
    return any(prefix.encode('utf-8') in raw.lower() for prefix in prefixes)


def validate_content(content):
    for name, raw in content.items():
        if any(part in ('runtime', 'studies', '.venv') for part in Path(name).parts) or name.endswith('.local.json'):
            raise ValueError('Private release input: ' + name)
        # SOURCE_PROVENANCE intentionally records historical provenance paths.
        if name != 'SOURCE_PROVENANCE.json' and machine_path_in_program(name, raw):
            raise ValueError('Machine-specific path in release code: ' + name)


def make_release_metadata(content, commit, dirty):
    return json.dumps({
        'name': 'Fenjue Windows local source release', 'version': '0.3.0',
        'source_commit': commit, 'source_dirty': dirty,
        'source_tree_sha256': tree_hash(content),
        'source_tree_hash_scope': 'all packaged inputs before generated metadata, including wheels',
        'python': '3.12 x64', 'inference_runs_during_install': 0,
        'credentials_included': False, 'quality_advantage_proven': False,
        'cockpit_patch': {
            'source_revision': '5273335ef9a96611336c4b927759c125a0f092d7',
            'binary_included': False,
            'build_prerequisites': 'Python 3.12 x64, fixed Cockpit source checkout and Go 1.26.9 windows/amd64; see scripts/Build-CockpitPatch.ps1',
        },
    }, indent=2, ensure_ascii=False).encode('utf-8')


def make_validation_metadata(commit, dirty):
    return json.dumps({
        'source_commit': commit, 'source_dirty': dirty,
        'current_source_tests': 'not_run_by_release_builder',
        'fresh_directory_install': 'not_run_by_release_builder',
        'live_generation': 'not_run_by_release_builder',
        'historical_validation': 'historical-release-validation.json',
    }, indent=2).encode('utf-8')


def write_archive(content, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, raw in sorted(content.items()):
            info = zipfile.ZipInfo('Fenjue-Windows/' + name, date_time=(2026, 10, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('Archive CRC check failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-dirty', action='store_true',
                        help='Allow development packaging; metadata records source_dirty=true.')
    args = parser.parse_args()
    status = git_status()
    dirty = bool(status)
    if dirty and not args.allow_dirty:
        raise ValueError('Working tree is dirty; commit the release inputs or pass --allow-dirty for development.')
    content = {name: path.read_bytes() for name, path in entries(args.wheelhouse).items()}
    validate_content(content)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if git_status() != status:
        raise ValueError('Working tree changed while packaging; retry after edits finish.')
    content['RELEASE.json'] = make_release_metadata(content, commit, dirty)
    # Building an archive does not re-run the old package's installation/tests.
    content['RELEASE-VALIDATION.json'] = make_validation_metadata(commit, dirty)
    content['MANIFEST.sha256'] = (
        '\n'.join(hashlib.sha256(raw).hexdigest() + '  ' + name for name, raw in sorted(content.items())) + '\n'
    ).encode('utf-8')
    write_archive(content, args.out)
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    args.out.with_suffix(args.out.suffix + '.sha256').write_text(
        digest + '  ' + args.out.name + '\n', encoding='utf-8')
    print(json.dumps({
        'archive': str(args.out.resolve()), 'files': len(content),
        'bytes': args.out.stat().st_size, 'sha256': digest, 'source_dirty': dirty,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
