"""Build a source + Windows dependency release using an explicit allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = ['README.md', 'DEPLOY-WINDOWS.md', '渠道进展_2026-10-07.md',
       'STATUS.md', 'REVIEW.md', 'PROGRESS.md', '使用焚决.md',
       '证实结果_2026-10-07.md', '站长接入与机制研究_2026-10-07.md',
       '协议验收摘要_2026-10-07.json', '协议研究来源_2026-10-07.json',
       'SOURCE_PROVENANCE.json', 'pyproject.toml', 'requirements.lock',
       'local.example.json', 'Install.cmd', 'Start-Workbench.cmd',
       'Stop-Workbench.cmd', '焚决.cmd', '.gitignore', 'RELEASE-VALIDATION.json']


def entries(wheelhouse):
    result = {name: ROOT / name for name in TOP}
    for folder, suffixes in [('fenjue', {'.py', '.html'}), ('tests', {'.py'}),
                             ('scripts', {'.py', '.ps1'}), ('protocol', {'.go'})]:
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and path.suffix in suffixes and '__pycache__' not in path.parts:
                result[path.relative_to(ROOT).as_posix()] = path
    wheels = list(wheelhouse.glob('*.whl'))
    if not wheels:
        raise ValueError('Wheelhouse is empty')
    result.update({'wheelhouse/' + p.name: p for p in wheels})
    for name, path in result.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError('Release input missing or symlink: ' + name)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    content = {name: path.read_bytes() for name, path in entries(args.wheelhouse).items()}
    # No machine-specific operational configuration or run state is inherited.
    for name, raw in content.items():
        if any(part in ('runtime', 'studies', '.venv') for part in Path(name).parts) or name.endswith('.local.json'):
            raise ValueError('Private release input: ' + name)
        machine_prefix = (Path.home().as_posix() + '/').encode()
        if name.endswith(('.py', '.ps1', '.cmd', '.json')) and machine_prefix in raw and name != 'SOURCE_PROVENANCE.json':
            raise ValueError('Machine-specific path in release code: ' + name)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    content['RELEASE.json'] = json.dumps({'name':'Fenjue Windows local source release',
        'version':'0.3.0', 'source_commit':commit, 'python':'3.12 x64',
        'inference_runs_during_install':0, 'credentials_included':False,
        'quality_advantage_proven':False}, indent=2).encode()
    manifest = '\n'.join(hashlib.sha256(raw).hexdigest() + '  ' + name for name, raw in sorted(content.items())) + '\n'
    content['MANIFEST.sha256'] = manifest.encode()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, raw in sorted(content.items()):
            info = zipfile.ZipInfo('Fenjue-Windows/' + name, date_time=(2026,10,7,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    with zipfile.ZipFile(args.out) as archive:
        if archive.testzip() is not None:
            raise ValueError('Archive CRC check failed')
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    args.out.with_suffix(args.out.suffix + '.sha256').write_text(digest + '  ' + args.out.name + '\n', encoding='utf-8')
    print(json.dumps({'archive':str(args.out.resolve()), 'files':len(content),
                      'bytes':args.out.stat().st_size, 'sha256':digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
