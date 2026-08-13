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


def test_derived_templates_are_repaired_and_migrated_before_use():
    script = _SCRIPT.read_text(encoding="utf-8")

    reversed_block = script.split('if guard proam_prod_mirror_p0rev; then', 1)[1].split('\nfi', 1)[0]
    assert reversed_block.index('prepare_current_schema proam_prod_mirror_p0rev') < (
        reversed_block.index('reverse_physical_order proam_prod_mirror_p0rev')
    )

    oracle_block = script.split('if guard proam_prod_mirror_mt; then', 1)[1].split('\nfi', 1)[0]
    assert oracle_block.index('prepare_current_schema proam_prod_mirror_mt') < (
        oracle_block.index('proam_regression.stage_multitournament')
    )


def test_reverse_order_supports_composite_primary_keys_without_shell_splitting():
    script = _SCRIPT.read_text(encoding="utf-8")

    assert 'string_agg(format(\'%I DESC\', a.attname)' in script
    assert 'while IFS= read -r statement' in script
    assert "read -r t pk" not in script
