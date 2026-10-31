"""``python -m tflens``: the same entry point as the ``tflens`` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
