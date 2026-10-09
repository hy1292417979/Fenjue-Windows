import json
from .candidates import candidate_inventory
from .config import ROUTES


def build_report(study):
    outcomes = {row['key']: row for row in study.db.rows(study.config['profile'])}
    exclusions = study.db.metadata('quality_exclusions') or {}
    pairs = []
    wins = {route: 0 for route in ROUTES}
    all_complete = True
    for task in study.tasks:
        arms = {route: outcomes.get(route + ':' + task['id']) for route in ROUTES}
        complete = all(row and row.get('identity_ok') and row.get('status') == 'completed'
                       and row['key'] not in exclusions
                       for row in arms.values())
        all_complete &= complete
        if complete:
            passed = [route for route, row in arms.items() if row['grade']['passed']]
            if len(passed) == 1:
                wins[passed[0]] += 1
        pairs.append({'task_id': task['id'], 'category': task['category'], 'family': task['family'],
                      'complete': complete, 'arms': arms})
    ceiling = bool(pairs) and all_complete and all(
        row['grade']['passed'] for pair in pairs for row in pair['arms'].values())
    if not all_complete:
        conclusion = 'incomplete_evidence'
    elif study.config['profile'] == 'smoke':
        conclusion = 'smoke_only'
    elif ceiling:
        conclusion = 'no_discrimination_ceiling'
    elif wins['codex_http'] > wins['sub2api']:
        conclusion = 'observed_candidate_requires_replication'
    else:
        conclusion = 'no_observed_candidate_advantage'
    calls = study.db.calls()
    return {'study_id': study.config['study_id'], 'profile': study.config['profile'],
            'model': study.config['model'], 'effort': study.config['effort'],
            'conclusion': conclusion, 'full_capability_proven': False, 'ceiling_effect': ceiling,
            'reserved_calls': len(calls), 'max_calls': study.config['max_calls'],
            'unfinished_calls': sum(c['record'] is None for c in calls),
            'identity': study.db.metadata('identity'), 'cleanup': study.db.metadata('cleanup'),
            'posthoc_audit': study.db.metadata('posthoc_audit'),
            'quality_exclusions': exclusions,
            'paired_wins': wins, 'pairs': pairs, 'calls': calls, 'candidates': candidate_inventory(),
            'limitations': ['No trusted full-capability reference; labels and reasoning tokens are not quality evidence.',
                           'One time window; related task variants are not independent samples.',
                           'Direct HTTP uses a custom client. Sub2API may rewrite requests after this tool sends them.',
                           'Client deadlines do not guarantee a server compute or subscription cost cap.',
                           'Local stored identity binding is not independent cryptographic identity attestation.',
                           'Coding category grades synthetic SQL contracts, not repository repair work.']}


def write_report(study):
    result = build_report(study)
    target = study.directory
    (target / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    labels = {
        'incomplete_evidence': '证据不完整：缺少配对结果或身份依据。',
        'smoke_only': '本次仅验证接入与基础 JSON 输出；尚未开展正式能力筛选。',
        'no_discrimination_ceiling': '未见区分，存在天花板效应。',
        'observed_candidate_requires_replication': '本轮直接 HTTP 配对胜题更多；仅为观察候选，需保留题及跨时间复验。',
        'no_observed_candidate_advantage': '本轮未观察到直接 HTTP 的配对胜题优势。',
    }
    lines = ['# 焚决渠道对照报告', '', labels[result['conclusion']], '',
             '**尚未证明任何渠道为“满血”。**', '',
             f"模型 / 推理强度：{result['model']} / {result['effort']}",
             f"调用预留：{result['reserved_calls']} / {result['max_calls']}；未知完成：{result['unfinished_calls']}",
             f"天花板效应：{'是' if result['ceiling_effect'] else '未确定或未出现'}", '',
             '| 题目 | 类别 / family | Sub2API | 直接 HTTP |', '|---|---|---|---|']
    for pair in result['pairs']:
        def label(route):
            row = pair['arms'][route]
            if not row:
                return '缺失'
            if row['key'] in result['quality_exclusions']:
                return '适配缺口，不计质量评分'
            return ('通过' if row['grade']['passed'] else '失败') + ' / ' + row['status'] + ('' if row.get('identity_ok') else ' / 身份缺口')
        lines.append(f"| {pair['task_id']} | {pair['category']} / {pair['family']} | {label('sub2api')} | {label('codex_http')} |")
    lines += ['', '## 调用证据', '', '| 渠道 | 终态 | 响应模型 / effort | 输入 / 输出 token | 秒 |', '|---|---|---|---|---|']
    for call in result['calls']:
        r = call['record'] or {}
        u = r.get('usage', {})
        lines.append(f"| {call['route_id']} | {r.get('status','未知完成')} | {r.get('model')} / {r.get('effort')} | {u.get('input_tokens')} / {u.get('output_tokens')} | {r.get('elapsed_seconds')} |")
    lines += ['', '## 身份与清理', '',
              '身份依据为本机指定账号的存储凭据、独占分组检查及中转用量关联；未做独立密码学身份背书。',
              '实际模型标签与 effort 仅作合同检查，不作为能力评分。',
              f"临时 Key 清理：{result['cleanup']}", '', '## 候选渠道', '', '| 渠道 | 状态 | 缺口 |', '|---|---|---|']
    lines.extend(f"| {c['route']} | {c['status']} | {c['gap']} |" for c in result['candidates'])
    if result['posthoc_audit']:
        lines += ['', '## 事后核查（保留首次执行结果）', '',
                  json.dumps(result['posthoc_audit'], ensure_ascii=False, indent=2), '']
    lines += ['', '## 解释边界', '',
              '- 未设置可信健康账号或官方同模型参考；本轮只有一个时间窗口。',
              '- 配对胜题数量不是统计显著性；同 family 的变体具有相关性。',
              '- SQL 题只覆盖合成数据库任务，尚未覆盖真实仓库修复。',
              '- 两端发出的公共请求体一致；Sub2API 转发后的内部改写仍可能不同。',
              '- OAuth 入口未提供本工具已验证的输出 token 硬上限；超时只约束客户端等待。',
              '- 正式筛选及跨时间复验需要独立冻结题集与调用预算。', '']
    (target / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return result
