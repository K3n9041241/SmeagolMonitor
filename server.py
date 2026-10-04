#!/usr/bin/env python3
"""Compatibility entry point; the implementation lives in servers.py."""
import sys
import servers

if __name__ == "__main__":
    servers.main()
else:
    sys.modules[__name__] = servers
