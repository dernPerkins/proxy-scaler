"""PyInstaller entry-point script — a real .py file is needed here since a
spec's Analysis takes a script path, not a console_scripts function
reference directly (unlike the `proxy-scaler-serve` pip entry point, which
setuptools wires straight to supervisor.cli_main).

Delegates to frozen_main() rather than cli_main() directly: the frozen
binary has to double as the Streamlit and worker children too (see
supervisor._child_command's docstring for why), and frozen_main() is what
dispatches an invocation to the right role."""

import sys

from proxy_scaler.supervisor import frozen_main

if __name__ == "__main__":
    sys.exit(frozen_main())
