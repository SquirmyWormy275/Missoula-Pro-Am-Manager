import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

WORKFLOW_PATH = Path(__file__).parents[1] / '.github' / 'workflows' / 'daily-backup.yml'


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding='utf-8')


def _step_block(workflow: str, name: str) -> str:
    marker = f'      - name: {name}'
    start = workflow.index(marker)
    next_step = workflow.find('\n      - name:', start + len(marker))
    if next_step == -1:
        return workflow[start:]
    return workflow[start:next_step]


def test_backup_restores_only_to_guarded_runner_local_postgres():
    workflow = _workflow_text()

    assert 'services:' in workflow
    assert 'image: postgres:18' in workflow
    assert 'RESTORE_HOST: 127.0.0.1' in workflow
    assert 'RESTORE_DB: proam_restore_verify' in workflow

    restore = _step_block(workflow, 'Restore dump into runner-local PostgreSQL')
    assert 'pg_restore' in restore
    assert '"$RESTORE_HOST" != "127.0.0.1"' in restore
    assert '"$RESTORE_DB" != "proam_restore_verify"' in restore
    assert 'RAILWAY' not in restore


def test_restore_requires_exact_migration_head_and_current_recovery_schema():
    workflow = _workflow_text()
    config = Config()
    config.set_main_option(
        'script_location',
        str(WORKFLOW_PATH.parents[2] / 'migrations'),
    )
    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1
    assert f'EXPECTED_ALEMBIC_HEAD: {heads[0]}' in workflow
    verify = _step_block(workflow, 'Verify restored schema and aggregate invariants')
    assert "version_num = '$EXPECTED_ALEMBIC_HEAD'" in verify
    assert 'score_submission_receipts' in verify
    assert 'owner_boot_id' in verify
    assert 'owner_heartbeat_at' in verify
    assert 'ix_score_submission_receipts_binding' in verify
    assert 'ix_background_jobs_owner_heartbeat_at' in verify
    assert "column_name = 'tournament_id' AND confdeltype = 'c'" in verify
    assert "column_name = 'issuing_user_id' AND confdeltype = 'n'" in verify
    assert "column_name = 'heat_id') = 0" in verify


def test_restore_password_is_scoped_to_trusted_database_steps():
    workflow = _workflow_text()
    job_configuration = workflow[workflow.index('jobs:') : workflow.index('    steps:')]

    assert 'PGPASSWORD:' not in job_configuration
    for step_name in (
        'Restore dump into runner-local PostgreSQL',
        'Verify restored schema and aggregate invariants',
        'Drop runner-local plaintext restore',
    ):
        step = _step_block(workflow, step_name)
        assert 'env:\n          PGPASSWORD: runner-local-restore-only' in step

    upload = _step_block(workflow, 'Upload encrypted backup artifact')
    assert 'PGPASSWORD' not in upload


def test_pg_restore_diagnostics_are_withheld_and_securely_deleted():
    workflow = _workflow_text()
    restore = _step_block(workflow, 'Restore dump into runner-local PostgreSQL')

    assert 'RESTORE_LOG=$(mktemp "$RUNNER_TEMP/proam_pg_restore_XXXXXX.log")' in restore
    assert 'chmod 600 "$RESTORE_LOG"' in restore
    assert 'trap secure_delete_restore_log EXIT' in restore
    assert '>"$RESTORE_LOG" 2>&1' in restore
    assert 'shred --remove=unlink --zero "$RESTORE_LOG"' in restore
    assert 'database diagnostics were withheld' in restore
    assert 'cat "$RESTORE_LOG"' not in restore
    assert 'tail ' not in restore


def test_production_dump_role_is_verified_without_row_output():
    workflow = _workflow_text()
    privilege_check = _step_block(workflow, 'Verify production dump role')

    assert 'rolsuper' in privilege_check
    assert 'default_transaction_read_only' in privilege_check
    assert 'has_table_privilege' in privilege_check
    assert 'RAILWAY_PG_READONLY_DUMP_URL' in privilege_check
    assert workflow.count('${{ secrets.RAILWAY_PG_READONLY_DUMP_URL }}') == 2
    assert 'RAILWAY_PG_PUBLIC_URL' not in workflow
    assert 'First 20 lines' not in workflow
    assert 'COPY public.alembic_version' not in workflow
    assert 'head -20' not in workflow


def test_verified_backup_is_encrypted_and_only_ciphertext_is_uploaded():
    workflow = _workflow_text()
    recipient_check = _step_block(workflow, 'Verify encryption recipient')
    encryption = _step_block(workflow, 'Encrypt verified backup')
    cleanup = _step_block(workflow, 'Delete plaintext backup')
    drop_restore = _step_block(workflow, 'Drop runner-local plaintext restore')
    upload = _step_block(workflow, 'Upload encrypted backup artifact')

    assert '${{ vars.BACKUP_AGE_RECIPIENT }}' in recipient_check
    assert workflow.index('Verify encryption recipient') < workflow.index('Take read-only pg_dump')
    assert 'age --recipient' in encryption
    assert 'if: ${{ always() }}' in cleanup
    assert 'shred' in cleanup
    assert 'Plaintext cleanup failed; artifact upload is blocked.' in cleanup
    assert 'exit 1' in cleanup
    assert 'if: ${{ success() }}' in upload
    assert 'path: ${{ env.CIPHERTEXT_FILE }}' in upload
    assert 'path: ${{ env.BACKUP_FILE }}' not in workflow
    assert 'dropdb' in drop_restore
    assert 'if: ${{ always() }}' in drop_restore
    assert '"$RESTORE_HOST" != "127.0.0.1"' in drop_restore
    assert '"$RESTORE_DB" != "proam_restore_verify"' in drop_restore
    assert workflow.index('Drop runner-local plaintext restore') < workflow.index(
        'Delete plaintext backup'
    )
    assert workflow.index('Delete plaintext backup') < workflow.index(
        'Upload encrypted backup artifact'
    )


def test_encryption_recipient_is_pinned_by_reviewable_fingerprint():
    workflow = _workflow_text()
    recipient_check = _step_block(workflow, 'Verify encryption recipient')

    assert '${{ vars.BACKUP_AGE_RECIPIENT_SHA256 }}' in recipient_check
    assert 'PINNED_RECIPIENT_SHA256' in recipient_check
    assert 'ACTUAL_RECIPIENT_SHA256' in recipient_check
    assert 'sha256sum' in recipient_check
    assert 'does not match the approved fingerprint' in recipient_check


def test_workflow_does_not_claim_ciphertext_recovery_was_verified():
    workflow = _workflow_text()
    summary = _step_block(workflow, 'Write verification summary')

    assert '## Plaintext Backup Restore Verified' in summary
    assert '## Encrypted Backup Verified' not in summary
    assert 'Ciphertext decrypt-and-restore: not run by this workflow' in summary
    assert 'separately held identity rehearsal' in summary


def test_plaintext_restore_is_dropped_before_pinned_third_party_action():
    workflow = _workflow_text()
    pinned_upload = (
        'uses: actions/upload-artifact@'
        'ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2'
    )

    uses_positions = [match.start() for match in re.finditer(r'^\s+uses:', workflow, re.MULTILINE)]
    assert uses_positions
    assert workflow.index('Drop runner-local plaintext restore') < min(uses_positions)
    assert pinned_upload in workflow
    assert 'actions/upload-artifact@v4' not in workflow
    assert re.search(r'actions/upload-artifact@[0-9a-f]{40}\b', workflow)


def test_workflow_does_not_claim_unapproved_race_weekend_dates():
    workflow = _workflow_text()

    assert "cron: '0 * 24-26 4 *'" not in workflow
    assert '2027 event dates are not configured' in workflow
