"""Entry usable both as ``python3 -m thabit_translator`` and as a plain script.

The Jellyfin plugin spawns this file directly inside the extracted library, so
neither the parent package nor a working directory on sys.path can be assumed -
hence the fallback to the flat import (script mode puts this package directory
on sys.path as sys.path[0]).
"""

try:
    from .app import main
except ImportError:  # executed as a plain script: <library>/thabit_translator/__main__.py
    from app import main

if __name__ == "__main__":
    main()
