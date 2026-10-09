"""Backward-compatible launcher for File Organizer."""

from organizer import *  # Re-export organizer functions for existing imports.


if __name__ == "__main__":
    raise SystemExit(main())
