"""Audit full native exports without reading credentials or modifying recordings."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path


def audit(recording, provider, backgrounds, *, shadow=False, csv_path=None):
    messages = recording['messages']
    session = recording['session']['id']
    assert recording['session']['status'] == 'completed', 'Session did not complete'
    forbidden = {'api_key', 'authorization', 'jev_key', 'typesafe_api_key'}

    def check_fields(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(key.lower() for key in value), 'Credential field in export'
            for item in value.values():
                check_fields(item)
        elif isinstance(value, list):
            for item in value:
                check_fields(item)

    check_fields(recording)
    contexts = [item for item in messages if item['kind'] == 'decision_context']
    assert contexts, 'No model contexts recorded'
    requests = {item['request_id']: item for item in contexts}
    assert len(requests) == len(contexts), 'Duplicate model request'
    windows = {(item['binding']['generation'], item['decision_window']) for item in contexts}
    assert len(windows) == len(contexts), 'A distinct window was submitted twice'
    selected = set()
    foregrounds = set()
    for item in contexts:
        assert item['requested'] == provider and item['shadow'] == shadow
        binding = item['binding']
        assert binding['session'] == session
        assert binding['window_id'] == item['decision_window']
        assert len(binding['processes']) == backgrounds
        selected.update(pair[0] for pair in binding['processes'])
        foregrounds.add(binding['foreground'][0])
        features = item['context']
        assert [row['label'] for row in features['backgrounds']] == [f'b{i+1}' for i in range(backgrounds)]
        assert len(features['trend']) <= 3
    assert selected.isdisjoint(foregrounds)
    proposals = [item for item in messages if item['kind'] == 'model_proposal']
    assert len({item['request_id'] for item in proposals}) == len(proposals)
    for item in proposals:
        assert item['request_id'] in requests
        assert item['decision_window'] == requests[item['request_id']]['decision_window']
        assert 0 <= item['latency_ms'] < 1000
    outcomes = {item['request_id'] for item in messages
                if item['kind'] in ('model_proposal', 'model_rejection') and item.get('request_id')}
    assert set(requests) <= outcomes, 'A dispatched context has no recorded outcome'
    references = {item['decision_window'] for item in messages if item['kind'] == 'rule_reference'}
    assert {item['decision_window'] for item in contexts} <= references
    commands = [item['command'] for item in messages
                if item['kind'] == 'control' and item['command']['op'] == 'priority']
    assert not any(command.get('workload_id') in foregrounds for command in commands)
    acknowledgements = [item for item in messages if item['kind'] == 'decision_acknowledgement']
    for item in acknowledgements:
        assert item['decision']['target'] in selected
        if item.get('model_request_id'):
            assert item['model_request_id'] in requests
    if shadow:
        assert not acknowledgements, 'Shadow issued an acknowledged intervention'
        assert not any(command.get('workload_id') in selected for command in commands), 'Shadow changed a background'
        assert all(item['nice'] == 0 for item in messages
                   if item['kind'] == 'sample' and item.get('workload_id') in selected | foregrounds)
    if csv_path:
        with csv_path.open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == len(messages), 'JSON/CSV row counts differ'
        for row, item in zip(rows, messages):
            payload = json.loads(row['payload'])
            assert row['session_id'] == session
            assert int(row['host_receipt_ns']) == item['host_receipt_ns']
            assert payload == {key: value for key, value in item.items() if key != 'host_receipt_ns'}
    latencies = sorted(item['latency_ms'] for item in proposals)
    return {'passed': True, 'session_id': session, 'provider': provider, 'shadow': shadow,
            'backgrounds': backgrounds, 'contexts': len(contexts), 'proposals': len(proposals),
            'actions': dict(Counter(item['proposal']['action'] for item in proposals)),
            'fallbacks': [item['reason'] for item in messages if item['kind'] == 'policy_fallback'],
            'verified_interventions': len(acknowledgements), 'messages': len(messages),
            'raw_records': sum(item['kind'] == 'raw' for item in messages),
            'model_identities': sorted({item['proposal']['model'] for item in proposals}),
            'p95_latency_ms': latencies[int(.95 * (len(latencies)-1))] if latencies else None,
            'maximum_latency_ms': max(latencies, default=None),
            'json_csv_parity': bool(csv_path), 'credential_fields_absent': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('recording', type=Path)
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--provider', choices=('jev', 'laya'), required=True)
    parser.add_argument('--backgrounds', type=int, required=True)
    parser.add_argument('--shadow', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.recording.read_text()), args.provider, args.backgrounds,
                   shadow=args.shadow, csv_path=args.csv)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
