"""sysmon — rig's machine-wide resource monitor, alerting, auto-heal and session restore.

rig-cli#622. Provisioned by :mod:`riglib.sysmon` as a ``RunAtLoad``/``KeepAlive`` LaunchAgent
that runs ``python3 -m sysmon_daemon run`` from ``~/.config/rig/sysmon``: rig copies this whole
package there file by file, so every module here is stdlib-only, imports its siblings only
relatively, and stays Python 3.9-compatible (``/usr/bin/python3`` may run it).
"""

__all__ = ["__version__"]

__version__ = "1"
