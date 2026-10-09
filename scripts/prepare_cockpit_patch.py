"""Reproducible mechanical overlay onto the exact upstream checkout."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'runtime/cockpit-source'
REVISION = '5273335ef9a96611336c4b927759c125a0f092d7'

def main():
    revision = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVISION:
        raise SystemExit('Unexpected Cockpit revision; do not patch an unknown version.')
    target = SOURCE / 'sidecars/cockpit-cliproxy'
    for source in (ROOT / 'cockpit-patch').glob('fenjue_*.go'):
        shutil.copyfile(source, target / source.name)
    path = target / 'relay_server.go'
    text = path.read_text(encoding='utf-8')
    old = '\trouter.Use(s.policy.middleware())\n'
    new = '\trouter.Use(fenjueCapture())\n' + old + '\trouter.Use(s.fenjueMiddleware())\n'
    if new not in text:
        if text.count(old) != 1:
            raise SystemExit('Cockpit router integration point changed.')
        path.write_text(text.replace(old, new), encoding='utf-8')
    print('Cockpit source overlay prepared; installed executable unchanged.')

if __name__ == '__main__':
    main()
