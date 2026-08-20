"""Module entrypoint so the Actor starts with `python -m src`."""

import asyncio

from .main import main

asyncio.run(main())
