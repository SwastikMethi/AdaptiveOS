"""Guest resource arithmetic; kernel ownership and writes stay in LinuxHost."""

import math

STEP = 64 * 1024 ** 2
MIN_LOW = 4 * STEP
POOL = 3 * 1024 ** 3
SAFETY = 4 * 1024 ** 3
MEMORY_PARENT = {'low': POOL, 'min': 0, 'high': 'max', 'max': SAFETY}
PARENT_BPS = 20_000_000
NETWORK_LIMITS = {'parent_bps': PARENT_BPS, 'ceil_bps': PARENT_BPS, 'leaf_packets': 256}


def high_for(footprint):
    if type(footprint) is not int or footprint < 0:
        raise ValueError('Invalid observed memory consumption')
    # Integer arithmetic keeps the rounded threshold stable across exports.
    return ((footprint + max(MIN_LOW, (footprint + 3) // 4) + STEP - 1) // STEP) * STEP


def initial_lows(highs):
    target = min(POOL // len(highs) // STEP * STEP,
                 min((v for v in highs.values() if v != 'max'), default=POOL))
    if target < MIN_LOW:
        raise ValueError('Memory profile cannot support the minimum protection target')
    return {key: target for key in highs}


def counters(value):
    return value if isinstance(value, dict) else {k: int(v) for k, v in (line.split() for line in value.splitlines())}


def pressured(value):
    rows = {line.split()[0]: dict(field.split('=') for field in line.split()[1:])
            for line in value.splitlines()}
    if not {'some', 'full'} <= rows.keys():
        return True
    some, full = (float(rows[key]['avg10']) for key in ('some', 'full'))
    return not all(math.isfinite(v) and v >= 0 for v in (some, full)) or some >= 1 or full >= .1


def memory_values(row):
    return {key: ('max' if row['memory_' + key] == 'max' else int(row['memory_' + key]))
            for key in ('low', 'min', 'high', 'max')}


def memory_profile(peaks):
    highs = {key: high_for(value) for key, value in peaks.items()}
    suitable = sum(highs.values()) <= SAFETY
    return {'suitable': suitable, 'peaks': peaks, 'highs': highs,
            'reason': 'profiled consumption including cache' if suitable else 'required headroom exceeds parent safety envelope',
            'consumption_is_exact_working_set': False}


def favor_memory(settings, foreground, importance):
    result = {key: dict(value) for key, value in settings.items()}
    front = result[foreground]
    if front['high'] == 'max' or front['low'] + STEP > front['high']:
        return result
    if POOL - sum(row['low'] for row in result.values()) < STEP:
        eligible = [key for key in result if key != foreground and result[key]['low'] - STEP >= MIN_LOW]
        if not eligible:
            return result
        donor = min(eligible, key=lambda key: (('Low', 'Normal', 'High').index(importance[key]),
                                               -result[key]['low'], key))
        result[donor]['low'] -= STEP
    front['low'] += STEP
    return result


def equal_shares(task_ids):
    ids = sorted(task_ids)
    share, remainder = divmod(10_000, len(ids))
    return {key: share + (index < remainder) for index, key in enumerate(ids)}


def favor_network(shares, foreground, importance):
    result = dict(shares)
    eligible = [key for key in result if key != foreground and result[key] >= 1000]
    if result[foreground] > 7500 or not eligible:
        return result
    donor = min(eligible, key=lambda key: (('Low', 'Normal', 'High').index(importance[key]), -result[key], key))
    result[donor] -= 500
    result[foreground] += 500
    return result
