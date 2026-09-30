"""``python3 -m sysmon_daemon`` (installed copy) / ``python3 -m riglib.sysmon_daemon`` (repo)."""

import sys

from .cli import main

sys.exit(main())
