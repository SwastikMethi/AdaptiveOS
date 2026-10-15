"""Root-owned entry point; isolated Python must not import the user's checkout."""

import sys

sys.path.insert(0, '/opt/adaptive-os-guest')
from adaptive_os.guest_linux.service import main

if __name__ == '__main__':
    raise SystemExit(main())
