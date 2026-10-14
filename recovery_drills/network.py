"""Enter only a new loopback-only namespace; never change the host network."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path


def isolated():
    # sysfs may still be the host mount; query this process's network namespace.
    interfaces = {name for _index, name in socket.if_nameindex()}
    return interfaces == {'lo'}


def main():
    if not isolated():
        raise RuntimeError('A new network namespace containing only loopback is required.')
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True, timeout=5)
    from recovery_drills.cli import main as cli_main
    return cli_main(sys.argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
