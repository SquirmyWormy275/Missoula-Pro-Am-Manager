"""Regression checks for the parity-rig bootstrap script.

These checks inspect the operator guidance only. They never invoke the script,
which would create local PostgreSQL mirrors from a private dump.
"""

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rig_bootstrap.sh"


def test_printed_regression_command_uses_configured_python():
    script = _SCRIPT.read_text(encoding="utf-8")

    assert 'SECRET_KEY=\\$(\\"$PY\\" -c' in script
    assert 'SECRET_KEY=\\$(python3 -c' not in script
