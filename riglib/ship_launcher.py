"""Single source of installed ship-launcher bytes for apply and drift checks."""
from pathlib import Path
from shlex import quote


def ship_launcher_text(source: Path) -> str:
    # Preserve the selected catalog symlink; do not pin its old physical version.
    canonical = (source / "ship.sh").absolute()
    return (
        "#!/bin/sh\n"
        "# Rig-managed launcher: keep ship beside its canonical runtime helpers.\n"
        f"export AGENT_TOOLS_ROOT={quote(str(canonical.parent.parent.parent))}\n"
        f"exec {quote(str(canonical))} \"$@\"\n"
    )
