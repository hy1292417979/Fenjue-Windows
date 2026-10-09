import hashlib
import json
import random
from pathlib import Path

from .config import read, validate, ROUTES
from ..research.storage import StudyDB, secure_directory, write_json_new
from ..research.tasks import build_suite


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(config_path, out):
    config = read(config_path)
    out = Path(out)
    if out.exists():
        raise FileExistsError(out)
    if config['profile'] == 'smoke':
        tasks = [dict(id='smoke-json', family='smoke', split='smoke', category='smoke',
                      prompt='Return exactly the JSON object {"sum":391,"sorted":[2,5,9]}. No other text.',
                      grader={'kind': 'exact_json', 'expected': {'sum': 391, 'sorted': [2, 5, 9]}},
                      tools=[], max_turns=1)]
    else:
        tasks = [t for t in build_suite(config['seed'])['tasks'] if t['split'] == config['profile']]
    rng = random.Random(config['seed'])
    rng.shuffle(tasks)
    schedule = []
    for task in tasks:
        routes = list(ROUTES)
        rng.shuffle(routes)
        schedule.extend({'route': r, 'task_id': task['id'], 'key': r + ':' + task['id']} for r in routes)
    secure_directory(out)
    for name, value in [('config', config), ('tasks', tasks), ('schedule', schedule)]:
        write_json_new(out / (name + '.json'), value)
    manifest = {name: digest(out / name) for name in ('config.json', 'tasks.json', 'schedule.json')}
    write_json_new(out / 'manifest.json', manifest)
    with open_study(out):
        pass
    return {'directory': str(out.resolve()), 'scheduled_tasks': len(schedule),
            'worst_case_calls': sum(t['max_turns'] for t in tasks) * 2, 'max_calls': config['max_calls']}


class Study:
    def __init__(self, out):
        self.directory = Path(out)
        manifest = json.loads((self.directory / 'manifest.json').read_text())
        if set(manifest) != {'config.json', 'tasks.json', 'schedule.json'}:
            raise ValueError('Invalid frozen manifest')
        if any(digest(self.directory / name) != sha for name, sha in manifest.items()):
            raise ValueError('Frozen material changed')
        self.config = validate(json.loads((self.directory / 'config.json').read_text()))
        self.tasks = json.loads((self.directory / 'tasks.json').read_text())
        self.schedule = json.loads((self.directory / 'schedule.json').read_text())
        stage = self.config['profile']
        self.db = StudyDB(self.directory, {'total': self.config['max_calls'], stage: self.config['max_calls']})
        frozen = digest(self.directory / 'manifest.json')
        previous = self.db.metadata('manifest')
        if previous is None:
            self.db.set_metadata_once('manifest', frozen)
        elif previous != frozen:
            self.db.close()
            raise ValueError('Manifest differs from ledger binding')

    def reserve(self, route_id, task_key):
        if route_id not in ROUTES:
            raise ValueError('Unknown route')
        return self.db.charge(self.config['profile'], route_id, task_key)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()


def open_study(out):
    return Study(out)
