"""PyInstaller entry-point script — a real .py file is needed here since a
spec's Analysis takes a script path, not a console_scripts function
reference directly (unlike the `proxy-scaler-serve` pip entry point, which
setuptools wires straight to supervisor.cli_main)."""

import sys

from proxy_scaler.supervisor import cli_main

if __name__ == "__main__":
    sys.exit(cli_main())
