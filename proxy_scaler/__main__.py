"""Allow `python -m proxy_scaler`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
