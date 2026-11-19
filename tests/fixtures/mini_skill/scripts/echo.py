"""Harmless sandbox fixture: echoes positional arguments."""

import sys

if __name__ == "__main__":
    print("ECHO:" + " ".join(sys.argv[1:]))
