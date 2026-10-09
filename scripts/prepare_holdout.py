"""Freeze two disjoint-family confirmation tasks within the original total 36-call cap."""
import json
from pathlib import Path
from fenjue.discovery.config import read
from fenjue.discovery.study import open_study, digest
from fenjue.research.storage import secure_directory, write_json_new
from fenjue.research.tasks import build_suite

with open_study('studies/astra-screen-proposal-20261007') as screen:
    assert len(screen.db.calls())==32
    assert not (screen.directory/'run.lock').exists()
config=read('screen.proposal.json')|{'study_id':'astra-holdout-20261007','profile':'confirm','max_calls':4}
ids=['confirm-constraints-01','confirm-constraints-02']
suite=build_suite(config['seed'])['tasks']
tasks=[next(t for t in suite if t['id']==identifier) for identifier in ids]
assert all(t['max_turns']==1 for t in tasks)
schedule=[]
for index,task in enumerate(tasks):
    for route in (('sub2api','codex_http') if index==0 else ('codex_http','sub2api')):
        schedule.append({'route':route,'task_id':task['id'],'key':route+':'+task['id']})
out=Path('studies/astra-holdout-20261007')
secure_directory(out)
for name,value in [('config',config),('tasks',tasks),('schedule',schedule)]:
    write_json_new(out/(name+'.json'),value)
write_json_new(out/'manifest.json',{name:digest(out/name) for name in ('config.json','tasks.json','schedule.json')})
write_json_new(out/'preregistration.json',{
    'total_new_call_cap':36,'already_reserved_in_screen':32,'holdout_call_cap':4,
    'selection':'first two confirm constraints tasks, ordered before any holdout answer',
    'purpose':'check direction of the screen checksum difference on unseen scheduling tasks',
    'independence':'two variants of one fresh family; analyze separately from screening',
    'success_criterion':'record first-answer exact pass/fail for each route; two wins are exploratory and do not prove long-term superiority',
    'no_retries':True,'no_reuse_of_failed_screen_questions':True})
with open_study(out) as study:
    study.db.set_metadata_once('aggregate_budget',{'screen_reserved':32,'this_cap':4,'combined_cap':36})
print(json.dumps({'prepared':str(out),'task_ids':ids,'families':[t['family'] for t in tasks],'combined_max_calls':36}))
