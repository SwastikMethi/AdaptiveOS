"""No sockets: one initialized provider and sequential private-pipe messages."""
import contextlib
import json
import os
import resource
import select
import sys
import threading
import time

from .model_providers import JevProvider, LayaProvider, ProviderError
from .model_runtime import MAX_PACKET


def watch_owner():
    """Exit on private-pipe closure or parent death, even during blocked inference."""
    owner = os.getppid()
    poller = select.poll()
    poller.register(sys.stdin.fileno(), select.POLLHUP | select.POLLERR | select.POLLNVAL)

    def supervise():
        while os.getppid() == owner:
            if poller.poll(100):
                os._exit(0)
        os._exit(0)

    threading.Thread(target=supervise, daemon=True, name='helper-owner').start()


def main():
    watch_owner()
    protocol_out = sys.stdout

    def emit(value):
        protocol_out.write(json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n')
        protocol_out.flush()

    def read():
        line = sys.stdin.buffer.readline(MAX_PACKET + 1)
        if not line:
            return None
        if len(line) > MAX_PACKET or not line.endswith(b'\n'):
            raise ValueError('input bound')
        return json.loads(line)

    with contextlib.redirect_stdout(sys.stderr):
        config = None
        try:
            config = read()
            started = time.monotonic()
            if config['provider'] == 'jev':
                if config.get('backend') == 'guest-linux':
                    from .guest_linux.policy import GuestJevProvider
                    provider = GuestJevProvider(config.pop('key', ''))
                else:
                    provider = JevProvider(config.pop('key', ''))
            elif config['provider'] == 'laya':
                provider = LayaProvider(config['checkpoint'], config['support'], config['device'])
            else:
                raise ProviderError('unavailable')
            loaded_at = time.monotonic()
            for context in config.get('warmups', [config['warmup']] if config.get('warmup') else []):
                provider.predict(context)
            emit({'kind': 'ready', 'cold_start_ms': (time.monotonic() - started) * 1000,
                  'load_ms': (loaded_at - started) * 1000, 'warmup_ms': (time.monotonic() - loaded_at) * 1000,
                  'device': config.get('device', 'hosted')})
        except Exception as exc:
            if config and config.get('provider') == 'laya' and '--diagnose-laya' in sys.argv:
                # Explicit build-time diagnostics only. Jev never prints exception text.
                import traceback
                traceback.print_exc(file=sys.stderr)
            emit({'kind': 'startup_error', 'error': exc.code if isinstance(exc, ProviderError) else 'unavailable'})
            return
        while True:
            packet = read()
            if packet is None:
                return
            request_id = packet.get('request_id')
            try:
                before = time.process_time()
                proposal = provider.predict(packet['context'])
                emit({'request_id': request_id, 'proposal': proposal,
                      'helper_cpu_seconds': time.process_time() - before,
                      'helper_max_rss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
            except Exception as exc:
                # Exception text may include a request/key/path. Only enumerated codes cross pipes.
                emit({'request_id': request_id, 'error': exc.code if isinstance(exc, ProviderError) else 'unavailable'})


if __name__ == '__main__':
    main()
