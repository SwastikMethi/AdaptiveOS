"""Deterministic desktop experiment manifests and paired reports."""

import random
import math
from statistics import mean


def calibration_key(config):
    """Calibration applies only to the exact workload, image and CPU placement."""
    sandbox = config.get('sandbox', {})
    required = ('image_id', 'workload_cpu', 'collector_cpu', 'allowed_cpus', 'shared_collector')
    if any(sandbox.get(key) is None for key in required) or not sandbox.get('image_id'):
        return None
    if any(config.get(key) is None for key in ('budget_ms', 'backgrounds', 'importance')):
        return None
    return {'budget_ms': config['budget_ms'], 'period_ms': 20,
            'backgrounds': config['backgrounds'], 'importance': config['importance'],
            'foreground_preset': 'periodic', 'background_preset': 'cpu',
            'sandbox': {key: sandbox[key] for key in required}}


def inference_summary(events, cold_start_ms=None):
    requests = sum(row['kind'] == 'decision_context' for row in events)
    replies = [row for row in events if row['kind'] == 'model_proposal']
    measured = replies + [row for row in events if row['kind'] == 'model_rejection'
                          and row.get('reason') == 'late_response']
    latency = sorted(row['latency_ms'] for row in replies if row.get('latency_ms') is not None)
    cpu = [row['helper_cpu_seconds'] for row in measured if row.get('helper_cpu_seconds') is not None]
    rss = [row['helper_max_rss'] for row in measured if row.get('helper_max_rss') is not None]
    tokens = [row['input_tokens'] for row in replies if row.get('input_tokens') is not None]
    return {'requests': requests, 'accepted_responses': len(replies),
            'timeouts': sum(row['kind'] == 'policy_fallback' and row.get('reason') == 'deadline' for row in events),
            'cold_start_ms': cold_start_ms,
            'warm_latency_ms': {name: latency[math.ceil(p * len(latency)) - 1] if latency else None
                               for name, p in (('p50', .5), ('p95', .95), ('p99', .99))},
            'observed_helper_cpu_seconds': sum(cpu) if cpu else (None if requests else 0),
            'cpu_measured_responses': len(cpu), 'helper_peak_rss_bytes': max(rss) if rss else None,
            'maximum_input_tokens': max(tokens) if tokens else None,
            'maximum_input_bytes': max((row.get('input_bytes', 0) for row in events), default=0)}


def _blocks(arms, count, seed):
    rng = random.Random(seed)
    rows = []
    for block in range(count):
        order = list(arms)
        rng.shuffle(order)
        rows.extend({'block': block, 'arm': arm, 'warmup_seconds': 5,
                     'measurement_seconds': 180} for arm in order)
    return rows


def trial_plan(kind, seed=20260930):
    if kind == 'resource_comparison':
        return [{**row, 'profile': profile, 'warmup_seconds': 30}
                for offset, profile in enumerate(('light', 'contention'))
                for row in _blocks(('default', 'manual', 'rule', 'jev'), 5, seed + offset)]
    if kind == 'resource_calibration':
        return [dict(profile=profile, block=rep, arm='default', warmup_seconds=30, measurement_seconds=30)
                for profile in ('light', 'contention') for rep in range(3)]
    if kind == 'model_comparison':
        return [{**row, 'profile': profile}
                for offset, profile in enumerate(('light', 'contention'))
                for row in _blocks(('default', 'manual10', 'rule', 'jev', 'laya'), 5, seed + offset)]
    if kind in ('shadow_jev', 'shadow_laya'):
        return [{'block': 0, 'profile': 'contention', 'arm': kind.replace('_', ':'),
                 'fixed_nice': 0, 'warmup_seconds': 5, 'measurement_seconds': 180}]
    if kind == 'heldout_calibration':
        return [{**row, 'block': row['repetition'], 'arm': row['profile'], 'measurement_seconds': 30}
                for row in calibration_plan() if row['budget_ms'] == 6]
    if kind == 'responsiveness':
        return [{**row, 'profile': profile}
                for offset, profile in enumerate(('light', 'contention'))
                for row in _blocks(('default', 'manual10', 'rule'), 5, seed + offset)]
    if kind == 'priority':
        return _blocks((0, 10, 19), 3, seed)
    if kind == 'coverage':
        return [{'block': 0, 'arm': 'rule', 'warmup_seconds': 5,
                 'measurement_seconds': 300, 'control_coverage_only': True}]
    if kind == 'calibration':
        return [{**row, 'block': row['repetition'], 'arm': row['profile'],
                 'measurement_seconds': 30} for row in calibration_plan()]
    raise ValueError('Unknown desktop experiment')


def calibration_plan():
    return [{'budget_ms': budget, 'profile': profile, 'repetition': repetition,
             'warmup_seconds': 5}
            for budget in (2, 4, 6, 8, 10, 12, 14, 16)
            for profile in ('solo', 'light', 'contention') for repetition in range(3)]


def qualifies_calibration(solo_miss_rate, light_miss_rate, contention_miss_rate,
                          contention_p95_lateness_ms):
    return (solo_miss_rate <= .005 and light_miss_rate <= .02 and
            (contention_miss_rate > .02 or contention_p95_lateness_ms > 1))


def _profile_report(trials):
    by_arm = {arm: [row for row in trials if row.get('arm') == arm and row.get('valid', True)]
              for arm in ('default', 'manual10', 'rule')}
    if not all(by_arm.values()):
        return {'useful': False, 'foreground_improvement_pct': None,
                'background_loss_pct': None, 'reason': 'default, manual10, and rule trials are required'}
    paired_blocks = ({row['block'] for row in by_arm['default']} &
                     {row['block'] for row in by_arm['manual10']} &
                     {row['block'] for row in by_arm['rule']})
    default = [row for row in by_arm['default'] if row['block'] in paired_blocks]
    rule = [row for row in by_arm['rule'] if row['block'] in paired_blocks]
    if len(paired_blocks) < 5:
        return {'useful': False, 'foreground_improvement_pct': None,
                'background_loss_pct': None, 'valid_paired_blocks': len(paired_blocks),
                'reason': 'five valid paired blocks are required'}
    metrics = []
    for name in ('miss_rate', 'p95_lateness_ms'):
        baseline_values = [row[name] for row in default if row.get(name) is not None]
        controlled_values = [row[name] for row in rule if row.get(name) is not None]
        if len(baseline_values) != len(default) or len(controlled_values) != len(rule):
            continue
        baseline, controlled = mean(baseline_values), mean(controlled_values)
        if baseline > 0:
            metrics.append(100 * (baseline - controlled) / baseline)
    improvement = max(metrics) if metrics else None
    baseline_bg = mean(row['background_throughput'] for row in default)
    controlled_bg = mean(row['background_throughput'] for row in rule)
    loss = 100 * (baseline_bg - controlled_bg) / baseline_bg if baseline_bg > 0 else None
    manual = [row for row in by_arm['manual10'] if row['block'] in paired_blocks]
    indexed = {arm: {row['block']: row for row in rows} for arm, rows in by_arm.items()}
    differences = []
    for block in sorted(paired_blocks):
        baseline = indexed['default'][block]
        differences.append({'block': block, **{
            f'{arm}_minus_default': {
                metric: indexed[arm][block][metric] - baseline[metric]
                for metric in ('miss_rate', 'p95_lateness_ms', 'background_throughput')}
            for arm in ('manual10', 'rule')}})
    return {'useful': improvement is not None and improvement >= 20 and loss is not None and loss <= 40,
            'foreground_improvement_pct': improvement, 'background_loss_pct': loss,
            'default_trials': len(default), 'rule_trials': len(rule),
            'manual10_trials': len(manual),
            'valid_paired_blocks': len(paired_blocks), 'paired_differences': differences}


def comparison_report(trials):
    profiles = sorted({row.get('profile') for row in trials if row.get('profile')})
    if not profiles:
        return _profile_report(trials)
    reporter = _model_profile_report if any(row.get('arm') in ('jev', 'laya') for row in trials) else _profile_report
    reports = {profile: reporter([row for row in trials if row.get('profile') == profile])
               for profile in profiles}
    if reporter == _model_profile_report:
        return {'useful': set(profiles) == {'light', 'contention'} and
                all(report['paired_complete'] for report in reports.values()) and reports['contention']['useful'],
                'primary_profile': 'contention', 'primary_metric': 'miss_rate', 'profiles': reports}
    return {'useful': set(profiles) == {'light', 'contention'} and
            all(report['useful'] for report in reports.values()), 'profiles': reports}


def _model_profile_report(trials, names=('default', 'manual10', 'rule', 'jev', 'laya')):
    paired = all(len([row for row in trials if row.get('arm') == arm and row['block'] == block
                          and row.get('valid')]) == 1 for arm in names for block in range(5))
    paired = paired and len(trials) == 5 * len(names)
    metrics = ('miss_rate', 'p95_lateness_ms', 'background_throughput', 'controller_cpu_pct')
    arms = {}
    for arm in names:
        rows = [row for row in trials if row.get('arm') == arm]
        valid = [row for row in rows if row.get('valid')]
        windows = sum(row.get('decision_windows', 0) for row in rows)
        tasks = {key for row in valid for key in row.get('per_task_throughput', {})}
        arms[arm] = {'trials': len(rows), 'valid_trials': len(valid),
            **{metric: mean(row[metric] for row in valid) if valid and
               all(row.get(metric) is not None for row in valid) else None for metric in metrics},
            'per_task_throughput': {key: mean(row['per_task_throughput'][key] for row in valid
                                            if key in row.get('per_task_throughput', {})) for key in tasks},
            'fallback_fraction': sum(row.get('fallback_windows', 0) for row in rows) / windows if windows else 0,
            'inference_overhead': [row.get('inference_overhead') for row in rows],
            'interventions': [row.get('intervention_events', []) for row in rows]}
    for arm in names:
        arms[arm]['per_task_loss_vs_default_pct'] = {
            key: 100 * (baseline - arms[arm]['per_task_throughput'][key]) / baseline
            if baseline > 0 and key in arms[arm]['per_task_throughput'] else None
            for key, baseline in arms['default']['per_task_throughput'].items()}
    providers = {}
    default, rule = arms['default'], arms['rule']
    for provider in (name for name in ('jev', 'laya') if name in names):
        model = arms[provider]
        baseline, controlled = default['miss_rate'], model['miss_rate']
        improvement = 100 * (baseline - controlled) / baseline if baseline and controlled is not None else None
        bg, model_bg = default['background_throughput'], model['background_throughput']
        loss = 100 * (bg - model_bg) / bg if bg and model_bg is not None else None
        beats_rule = controlled is not None and rule['miss_rate'] is not None and controlled < rule['miss_rate']
        providers[provider] = {'useful': bool(paired and beats_rule and improvement is not None and
                                                improvement >= 20 and loss is not None and loss <= 40),
            'miss_rate_improvement_pct': improvement, 'background_loss_pct': loss,
            'improves_over_rule': beats_rule,
            'paired_miss_differences_vs_rule': [
                next(row['miss_rate'] for row in trials if row['arm'] == provider and row['block'] == block) -
                next(row['miss_rate'] for row in trials if row['arm'] == 'rule' and row['block'] == block)
                for block in range(5)] if paired else []}
    return {'report_version': 2, 'primary_metric': 'miss_rate', 'paired_complete': paired,
            'arms': arms, 'providers': providers, 'useful': any(row['useful'] for row in providers.values())}


def _counter_intervals(records, workload_id, reasons):
    streams = {}
    for envelope in records:
        if envelope.get('workload_id') != workload_id:
            continue
        row = envelope.get('data', {})
        fields = ('sequence', 'start_ns', 'mono_ns', 'units')
        if any(type(row.get(key)) is not int or row[key] < 0 for key in fields):
            reasons.append(f'invalid raw counter: {workload_id}')
            continue
        stream = streams.setdefault(row['start_ns'], {})
        key = row['sequence']
        value = tuple(row[field] for field in fields)
        if key in stream and stream[key] != value:
            reasons.append(f'conflicting raw counter: {workload_id}')
        stream[key] = value
    intervals = []
    for stream in streams.values():
        ordered = [stream[key] for key in sorted(stream)]
        for before, after in zip(ordered, ordered[1:]):
            if after[2] <= before[2] or after[3] < before[3]:
                reasons.append(f'non-monotonic raw counter: {workload_id}')
                continue
            if after[0] != before[0] + 1:
                # Never interpolate across a lost packet.
                continue
            intervals.append((before[2], after[2], after[3] - before[3]))
    return intervals


def _sample_intervals(samples, workload_id, reasons):
    """Compatibility for old callers without raw evidence, never new trials."""
    unique = {}
    for row in samples:
        if row.get('workload_id') != workload_id or row.get('app_units_delta') is None:
            continue
        left, right = row.get('app_interval_start_ns'), row.get('app_interval_end_ns')
        units = row['app_units_delta']
        if (type(left) is not int or type(right) is not int or right <= left or
                not isinstance(units, (int, float)) or not math.isfinite(units) or units < 0):
            reasons.append(f'invalid sampled interval: {workload_id}')
            continue
        key = (left, right)
        if key in unique and unique[key] != units:
            reasons.append(f'conflicting sampled interval: {workload_id}')
        unique[key] = units
    return [(left, right, units) for (left, right), units in unique.items()]


def summarize_trial(ticks, samples, background_ids, start_ns, end_ns, *, raw_records=None):
    """Versioned report from exact ticks and unique process-counter intervals.

    Supplying raw_records (even empty) never falls back to sampled chart data.
    The process start timestamp separates restarts of the same workload ID.
    """
    if type(start_ns) is not int or type(end_ns) is not int or end_ns <= start_ns:
        raise ValueError('measurement boundaries must be increasing integer timestamps')
    reasons = []
    ordered = []
    if ticks is not None:
        by_time = {}
        for tick in ticks:
            at = tick['scheduled_ns']
            if start_ns <= at < end_ns:
                if at in by_time and by_time[at] != tick:
                    reasons.append('conflicting foreground tick')
                by_time[at] = tick
        ordered = [by_time.get(scheduled) for scheduled in
                   range(start_ns, end_ns, 20_000_000)]
        if any(tick is None for tick in ordered):
            reasons.append('incomplete foreground tick coverage')

    per_task, coverage = {}, {}
    duration = end_ns - start_ns
    for workload_id in background_ids:
        units = covered = 0
        intervals = (_counter_intervals(raw_records, workload_id, reasons) if raw_records is not None
                     else _sample_intervals(samples, workload_id, reasons))
        previous_end = start_ns
        for interval_start, interval_end, delta in sorted(intervals):
            left, right = max(start_ns, interval_start), min(end_ns, interval_end)
            if right <= left:
                continue
            if left < previous_end:
                reasons.append(f'overlapping background intervals: {workload_id}')
                continue
            previous_end = right
            covered += right - left
            units += delta * (right - left) / (interval_end - interval_start)
        coverage[workload_id] = covered
        if covered < duration * .95:
            reasons.append(f'incomplete background throughput coverage: {workload_id}')
        elif covered:
            per_task[workload_id] = units * 1e9 / covered

    valid = not reasons
    complete = [tick for tick in ordered if tick is not None]
    lateness = sorted(max(0, tick['completion_ns'] - tick['deadline_ns']) / 1e6 for tick in complete)
    cpu_samples = [row for row in samples if row.get('mono_ns') is not None
                   and start_ns <= row['mono_ns'] < end_ns and row.get('cpu_pct') is not None]
    cpu = {workload_id: mean(row['cpu_pct'] for row in cpu_samples
                             if row.get('workload_id') == workload_id)
           for workload_id in {row.get('workload_id') for row in cpu_samples} if workload_id}
    return {'report_version': 2,
            'throughput_source': 'raw-cumulative-v1' if raw_records is not None else 'unique-samples-v1',
            'background_coverage_ns': coverage,
            'valid': valid, 'exclusion_reasons': sorted(set(reasons)),
            'miss_rate': (sum(tick['skipped'] or tick['completion_ns'] > tick['deadline_ns']
                              for tick in complete) / len(complete)) if valid and complete else None,
            'p95_lateness_ms': lateness[math.ceil(.95 * len(lateness)) - 1]
            if valid and lateness else None,
            'background_throughput': sum(per_task.values()) if valid else None,
            'per_task_throughput': per_task, 'cpu_allocation_pct': cpu}


def recompute_recording(recording):
    """Return new evidence; never mutate the historical recording/results."""
    results = []
    raw, samples = [], []
    for message in recording['messages']:
        if message.get('kind') == 'raw':
            raw.append(message)
        elif message.get('kind') == 'sample':
            samples.append(message)
        elif message.get('kind') == 'trial_boundary' and message.get('boundary') == 'warmup_start':
            raw, samples = [], []
        elif message.get('kind') == 'trial_boundary' and message.get('boundary') == 'measurement_end':
            original = message['result']
            if original.get('resource') in ('memory', 'network'):
                from .resource_experiments import summarize_resource
                metrics = summarize_resource(raw, samples, original['foreground_id'], set(original['background_ids']),
                    message['measurement_start_ns'], message['measurement_end_ns'], original['resource'])
                results.append({**original, **metrics, 'trial_index': message['trial_index']})
                continue
            ticks = [tick for envelope in raw for tick in envelope['data'].get('ticks', ())]
            backgrounds = set(original.get('per_task_throughput', {}))
            # Include missing streams excluded from an earlier sample-only report.
            backgrounds.update(envelope['workload_id'] for envelope in raw
                               if not envelope['data'].get('ticks') and any(
                                   task['id'] == envelope['workload_id'] and task.get('preset') != 'periodic'
                                   for task in recording.get('workloads', ())))
            metrics = summarize_trial(ticks if original.get('miss_rate') is not None or ticks else None,
                                      samples, backgrounds, message['measurement_start_ns'],
                                      message['measurement_end_ns'], raw_records=raw)
            results.append({**original, **metrics, 'trial_index': message['trial_index']})
    resource = recording.get('session', {}).get('metadata', {}).get('resource', 'cpu')
    if resource != 'cpu':
        from .resource_experiments import resource_comparison
        comparison = resource_comparison(results, resource, frozen=False)
    else:
        comparison = comparison_report(results)
    return {'report_version': 2, 'source_schema_version': recording.get('schema_version'),
            'source_session': recording.get('session', {}).get('id'),
            'results': results, 'comparison': comparison}
