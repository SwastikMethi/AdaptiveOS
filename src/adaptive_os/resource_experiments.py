"""Resource trial reports from complete, deduplicated five-second raw windows."""
from .experiments import summarize_trial, _model_profile_report
from .sandbox.resources import MeasurementWindow


def resource_calibration_key(config):
    sandbox = config.get('sandbox', {})
    if not sandbox.get('image_id') or config.get('resource') not in ('memory', 'network'):
        return None
    return {key: config.get(key) for key in ('resource', 'backgrounds', 'importance', 'link_mbps', 'workload_configs', 'profiles', 'sandbox')}


def summarize_resource(raw, samples, foreground_id, background_ids, start, end, resource):
    result = summarize_trial(None, samples, background_ids, start, end, raw_records=raw)
    windows = {}
    reasons = result['exclusion_reasons']
    for row in raw:
        if row.get('workload_id') != foreground_id:
            continue
        window = row.get('data', {}).get('resource_window')
        if not window or not start <= window.get('window_start_ns', -1) < end:
            continue
        at = window['window_start_ns']
        counts = [window.get(key) for key in ('requests', 'misses', 'failed_transfers')]
        histogram = window.get('latency_histogram', [])
        if (window.get('window_end_ns') != at + 5_000_000_000 or
                any(type(n) is not int or n < 0 for n in counts) or counts[0] == 0 or
                counts[1] > counts[0] or counts[2] > counts[0] or
                len(histogram) != len(MeasurementWindow.bounds_ms) or
                any(type(n) is not int or n < 0 for n in histogram) or sum(histogram) != counts[0]):
            reasons.append('invalid resource window')
            continue
        if at in windows and windows[at] != window:
            reasons.append('conflicting resource window')
        windows[at] = window
    if set(windows) != set(range(start, end, 5_000_000_000)):
        reasons.append('incomplete foreground resource windows')
    requests = sum(w['requests'] for w in windows.values())
    p95 = None
    accumulated = 0
    for index, bound in enumerate(MeasurementWindow.bounds_ms):
        accumulated += sum(w['latency_histogram'][index] for w in windows.values())
        if requests and accumulated >= .95 * requests:
            p95 = min(bound, max(w['p95_latency_ms'] for w in windows.values()))
            break
    result.update(resource=resource, primary_metric='cache_miss_rate' if resource == 'memory' else 'deadline_miss_rate',
        foreground_id=foreground_id, background_ids=sorted(background_ids), valid=not reasons,
        exclusion_reasons=sorted(set(reasons)), requests=requests,
        failed_transfers=sum(w['failed_transfers'] for w in windows.values()),
        miss_rate=sum(w['misses'] for w in windows.values()) / requests if requests and not reasons else None,
        p95_lateness_ms=p95 if not reasons else None, p95_completion_latency_ms=p95 if not reasons else None,
        throughput_unit='requests/s' if resource == 'memory' else 'bytes/s')
    return result


def resource_comparison(trials, resource, *, frozen):
    profiles = {profile: _model_profile_report([r for r in trials if r.get('profile') == profile],
                                               names=('default', 'manual', 'rule', 'jev'))
                for profile in ('light', 'contention')}
    return dict(resource=resource, frozen=bool(frozen), profiles=profiles,
        primary_metric='cache_miss_rate' if resource == 'memory' else 'deadline_miss_rate',
        useful=bool(frozen and all(r['paired_complete'] for r in profiles.values()) and profiles['contention']['useful']))
