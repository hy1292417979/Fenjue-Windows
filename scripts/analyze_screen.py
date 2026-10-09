"""Descriptive first-answer analysis; related variants aggregate at family level."""
import json
import math
import statistics
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from fenjue.discovery.study import open_study

STUDY=Path('studies/astra-screen-proposal-20261007')
ROUTES=('sub2api','codex_http')


with open_study(STUDY) as study:
    rows={row['key']:row for row in study.db.rows('screen')}
    calls=study.db.calls()
    exclusions=study.db.metadata('quality_exclusions') or {}
    paired=[];missing=[];family=defaultdict(list)
    for task in study.tasks:
        arms={r:rows.get(r+':'+task['id']) for r in ROUTES}
        if all(row and row['identity_ok'] and row['status']=='completed' and row['key'] not in exclusions for row in arms.values()):
            scores={r:int(row['grade']['passed']) for r,row in arms.items()}
            pair={'task':task['id'],'family':task['family'],'category':task['category'],'scores':scores,
                  'seconds':{r:row['elapsed_seconds'] for r,row in arms.items()}}
            paired.append(pair);family[task['family']].append(scores)
        else:
            missing.append({'task':task['id'],'status':{r:('adapter_gap' if row['key'] in exclusions else row['status']) if row else 'missing' for r,row in arms.items()}})
    family_scores={name:{r:sum(x[r] for x in values)/len(values) for r in ROUTES} for name,values in family.items()}
    signs=[(v['codex_http']>v['sub2api'])-(v['codex_http']<v['sub2api']) for v in family_scores.values()]
    direct_wins=signs.count(1);sub_wins=signs.count(-1);n=direct_wins+sub_wins
    sign_p=min(1.,2*sum(math.comb(n,i) for i in range(min(direct_wins,sub_wins)+1))/(2**n)) if n else 1.
    summary={
        'new_calls':len(calls),'max_calls':36,'complete_pairs':len(paired),'planned_pairs':len(study.tasks),
        'missing_pairs':missing,'family_count_observed':len(family),
        'paired_correct':{r:sum(p['scores'][r] for p in paired) for r in ROUTES},
        'paired_only_wins':{r:sum(p['scores'][r]>p['scores'][next(x for x in ROUTES if x!=r)] for p in paired) for r in ROUTES},
        'both_correct':sum(all(p['scores'].values()) for p in paired),
        'both_wrong':sum(not any(p['scores'].values()) for p in paired),
        'family_scores':family_scores,'family_exact_sign_p_descriptive':sign_p,
        'quality_inference':'incomplete protocol coverage' if missing else 'single-window exploratory comparison',
        'median_task_seconds':{r:statistics.median(p['seconds'][r] for p in paired) if paired else None for r in ROUTES},
        'status_counts':{r:dict(Counter(row['status'] for row in rows.values() if row['route']==r)) for r in ROUTES},
        'usage_sum':{r:{metric:sum((c['record'] or {}).get('usage',{}).get(metric,0) for c in calls if c['route_id']==r)
                        for metric in ('input_tokens','output_tokens')} for r in ROUTES},
        'formats':dict(Counter((c['record'] or {}).get('wire_format') or 'not_recorded' for c in calls)),
        'cleanup':study.db.metadata('cleanup'),'paired_tasks':paired,
        'quality_exclusions':exclusions,
        'limitations':['Missing pairs are not a loss for either channel.',
                       'Task variants within a family are correlated; 12 tasks are only 6 families.',
                       'One account, one time window, no trusted healthy-account baseline.',
                       'Direct response parser changed after the first protocol failure; failed question was not replayed.',
                       'No observed difference is not evidence of equivalence or full capability.']}
    (STUDY/'comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    error_task=next(t for t in study.tasks if t['id']=='screen-tools-03')
    error_row=rows.get('sub2api:screen-tools-03')
    if error_row and error_row['grade']['reason']=='answer_mismatch':
        records=error_task['grader']['records']
        manifest=records[error_task['grader']['required_record_ids'][0]]
        policy=records[manifest['policy_record_id']]
        versions=defaultdict(list)
        for shard_index,rid in enumerate(manifest['ledger_record_ids']):
            for row_index,row in enumerate(records[rid]['items']):
                versions[row['line_id']].append((row['revision'],shard_index,row_index,row))
        current=[max(v,key=lambda x:x[:3])[3] for v in versions.values()]
        selected=sorted([r for r in current if r['status']=='approved' and r['region'] in policy['allowed_regions']
                         and r['quantity']>=policy['minimum_quantity']],key=lambda r:r['line_id'])
        details=[{k:r[k] for k in ('line_id','quantity','unit_cents','fee_cents')}|
                 {'rebate_per_unit_cents':policy['rebate_per_unit_cents'],
                  'net_cents':r['quantity']*(r['unit_cents']-policy['rebate_per_unit_cents'])+r['fee_cents']} for r in selected]
        total=sum(r['net_cents'] for r in details)
        assert total==error_task['grader']['expected']['total_cents']
        (STUDY/'tool-error-evidence.json').write_text(json.dumps({'task':error_task['id'],'line_calculations':details,
            'independently_recomputed_cents':total,'model_answer':json.loads(error_row['answer']),
            'counterpart_excluded':True},ensure_ascii=False,indent=2),encoding='utf-8')
    wins=summary['paired_only_wins']
    verdict=('有效配对中未观察到答题正确率差异；这不构成长期等效或满血证明。'
             if not any(wins.values()) else '本轮观察到配对正确性差异；需复验后再判断是否存在稳定渠道优势。')
    lines=['# 同账号渠道表现对照 · 2026-10-07','',verdict,'',
        f"新调用 {len(calls)} / 36；有效配对 {len(paired)} / {len(study.tasks)}；观察到 {len(family)} 个任务 family。",'',
        '| 指标 | Sub2API | 直接 Codex HTTP |','|---|---:|---:|',
        f"| 有效配对内正确题数 | {summary['paired_correct']['sub2api']} | {summary['paired_correct']['codex_http']} |",
        f"| 仅本渠道答对 | {summary['paired_only_wins']['sub2api']} | {summary['paired_only_wins']['codex_http']} |",
        f"| 配对题总耗时中位数（秒） | {summary['median_task_seconds']['sub2api']} | {summary['median_task_seconds']['codex_http']} |",'',
        f"两端都正确 {summary['both_correct']} 题；两端都错误 {summary['both_wrong']} 题。",'',
        '## 每题证据','','| 任务 | family | Sub2API | 直接 HTTP |','|---|---|---|---|']
    for p in paired:
        lines.append(f"| {p['task']} | {p['family']} | {'正确' if p['scores']['sub2api'] else '错误'} | {'正确' if p['scores']['codex_http'] else '错误'} |")
    for p in missing:
        lines.append(f"| {p['task']} | 配对缺口 | {p['status']['sub2api']} | {p['status']['codex_http']} |")
    lines+=['','## 结论边界','',
        f"按 family 聚合的探索性双侧精确符号检验 p={sign_p:.4f}。样本很小，且本轮存在缺失或协议修订；此值不作为长期优越性或满血证明。",'',
        '缺失配对不记作任一渠道质量失败；每个任务只取第一次尝试。数据完整性问题单列。',
        '只有本账号，尚无独立正常参考。一次未见差异，不表示两个渠道永远等效，也不表示达到模型全部能力。',
        '本轮没有切换现有客户端流量、修改账号映射或更换模型。',
        '额外单侧记录：Sub2API 的 screen-tools-03 计算出 7966 分，独立重算为 7964 分；其直接渠道对照存在适配缺口，所以不计入渠道胜负。详见 tool-error-evidence.json。',
        '直接接口实测缺少 Content-Type，且部分 response.completed.output 为空；真实输出位于 response.output_item.done。Sub2API 已聚合这类输出。本工具现已补齐同样的解析。',
        f"临时 Key 清理：{summary['cleanup']}",'']
    (STUDY/'comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('paired_tasks','family_scores','limitations')},ensure_ascii=False))
