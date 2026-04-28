"""Domain conflict registry backed by a Git-tracked JSON file."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app


VALID_STATUSES = {
    'needs_decision': 'Needs Decision',
    'accepted_contract': 'Accepted Contract',
    'stale_doc': 'Stale Doc',
    'needs_test': 'Needs Test',
    'needs_code_fix': 'Needs Code Fix',
    'implemented': 'Implemented',
    'resolved': 'Resolved',
    'deferred': 'Deferred',
}

VALID_ACTIONS = {
    'accept_contract': 'accepted_contract',
    'mark_stale_doc': 'stale_doc',
    'needs_alex_decision': 'needs_decision',
    'needs_test': 'needs_test',
    'needs_code_fix': 'needs_code_fix',
    'mark_implemented': 'implemented',
    'mark_resolved': 'resolved',
    'defer': 'deferred',
}

VALID_SEVERITIES = ['critical', 'high', 'medium', 'low']


def default_registry_path() -> Path:
    configured = current_app.config.get('DOMAIN_CONFLICTS_PATH')
    if configured:
        return Path(configured)
    return Path(current_app.root_path) / 'docs' / 'domain_conflicts.json'


def load_registry(path: str | os.PathLike | None = None) -> dict:
    registry_path = Path(path) if path else default_registry_path()
    if not registry_path.exists():
        return {'schema_version': 1, 'conflicts': []}
    with registry_path.open('r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get('conflicts'), list):
        raise ValueError(f'Invalid domain conflict registry: {registry_path}')
    return data


def save_registry(registry: dict, path: str | os.PathLike | None = None) -> None:
    registry_path = Path(path) if path else default_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = registry_path.with_suffix(registry_path.suffix + '.tmp')
    with tmp_path.open('w', encoding='utf-8') as handle:
        json.dump(registry, handle, indent=2)
        handle.write('\n')
    os.replace(tmp_path, registry_path)


def list_conflicts(
    *,
    status: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    query: str | None = None,
    path: str | os.PathLike | None = None,
) -> tuple[list[dict], dict]:
    registry = load_registry(path)
    conflicts = [deepcopy(item) for item in registry.get('conflicts', [])]

    if status:
        conflicts = [item for item in conflicts if item.get('status') == status]
    if category:
        conflicts = [item for item in conflicts if item.get('category') == category]
    if severity:
        conflicts = [item for item in conflicts if item.get('severity') == severity]
    if query:
        needle = query.strip().lower()
        if needle:
            conflicts = [
                item for item in conflicts
                if needle in _search_text(item)
            ]

    conflicts.sort(key=_sort_key)
    return conflicts, summarize(registry.get('conflicts', []))


def summarize(conflicts: list[dict]) -> dict:
    by_status = {key: 0 for key in VALID_STATUSES}
    by_severity = {key: 0 for key in VALID_SEVERITIES}
    categories: dict[str, int] = {}
    for item in conflicts:
        status = item.get('status') or 'needs_decision'
        severity = item.get('severity') or 'medium'
        category = item.get('category') or 'uncategorized'
        by_status[status] = by_status.get(status, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1
        categories[category] = categories.get(category, 0) + 1
    return {
        'total': len(conflicts),
        'by_status': by_status,
        'by_severity': by_severity,
        'categories': dict(sorted(categories.items())),
    }


def update_conflict(
    conflict_id: str,
    *,
    status: str | None = None,
    action: str | None = None,
    decision: str | None = None,
    decision_note: str | None = None,
    actor: str | None = None,
    path: str | os.PathLike | None = None,
) -> dict:
    registry = load_registry(path)
    conflicts = registry.get('conflicts', [])
    target = next((item for item in conflicts if item.get('id') == conflict_id), None)
    if target is None:
        raise KeyError(conflict_id)

    resolved_status = VALID_ACTIONS.get(action or '') or status
    if resolved_status:
        if resolved_status not in VALID_STATUSES:
            raise ValueError(f'Invalid conflict status: {resolved_status}')
        target['status'] = resolved_status

    if decision is not None:
        target['decision'] = decision.strip()
    if decision_note is not None:
        target['decision_note'] = decision_note.strip()

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    target['updated_at'] = now
    if actor:
        target['updated_by'] = actor

    save_registry(registry, path)
    return deepcopy(target)


def _search_text(item: dict) -> str:
    parts = [
        item.get('id', ''),
        item.get('title', ''),
        item.get('category', ''),
        item.get('severity', ''),
        item.get('status', ''),
        item.get('contract_rule', ''),
        item.get('proposed_resolution', ''),
        item.get('decision', ''),
        item.get('decision_note', ''),
    ]
    for source in item.get('conflicting_sources', []) or []:
        parts.extend([
            source.get('file', ''),
            str(source.get('line', '')),
            source.get('text', ''),
        ])
    for test_id in item.get('test_coverage', []) or []:
        parts.append(str(test_id))
    return ' '.join(str(part) for part in parts).lower()


def _sort_key(item: dict):
    severity_rank = {name: idx for idx, name in enumerate(VALID_SEVERITIES)}
    status_rank = {
        'needs_decision': 0,
        'needs_code_fix': 1,
        'needs_test': 2,
        'accepted_contract': 3,
        'stale_doc': 4,
        'implemented': 5,
        'resolved': 6,
        'deferred': 7,
    }
    return (
        status_rank.get(item.get('status'), 99),
        severity_rank.get(item.get('severity'), 99),
        item.get('category') or '',
        item.get('title') or '',
    )
