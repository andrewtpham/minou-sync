"""`python -m minou_sync`, the same as the minou-sync command."""

import sys

from .cli import main

sys.exit(main())
