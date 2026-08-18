"""
Partner matching helpers for pro partnered events.

DOMAIN_CONTRACT (2026-04-27): the partner resolver must:

  1. Use the fuzzy matching ladder from services.name_match (exact →
     first-token → Levenshtein ≤ 2 on full name → Levenshtein ≤ 2 on
     first-token) so typos and first-name-only entries resolve. The plain
     normalized-name lookup that lived here previously missed every common
     race-week typo (Mckinley/Mickinley, Elise/Eloise, Kayla/Kaylah).

  2. Track CLAIMS before throwing anyone into the unclaimed pool. If A
     listed B as their partner, B is "claimed by A" — even if B's own
     partner field is blank or wrong. Without this, the auto-pair pass
     happily pairs B with some random C, then the operator has to manually
     unwind two broken partnerships at race time.

  3. Auto-pair only the genuinely unclaimed pool (competitors with no
     partner field AND no inbound claim from anyone). Mixed-gender events
     prefer M/F pairings; same-gender events pair within gender.

  4. Surface three distinct outcomes — confirmed reciprocal pairs, newly
     auto-paired, and one-sided claims that need operator review — so the
     UI can show a click-path for each remaining problem rather than a
     single ambiguous "unmatched" count.
"""

from __future__ import annotations

import hashlib
import json
import re

from database import db
from models import Event, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import EventResult
from services.name_match import find_partner_match, normalize_alphanum


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _is_entered(event: Event, entered_events: list) -> bool:
    target_id = str(event.id)
    target_name = _normalize_name(event.name)
    target_display = _normalize_name(event.display_name)

    for raw in entered_events or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in {target_name, target_display}:
            return True
    return False


def _read_partner_name(comp, event: Event) -> str:
    partners = comp.get_partners()
    if not isinstance(partners, dict):
        return ""
    for key in [
        str(event.id),
        event.name,
        event.display_name,
        event.name.lower(),
        event.display_name.lower(),
    ]:
        value = str(partners.get(key, "")).strip()
        if value:
            return value
    return ""


def set_partner_bidirectional(a, b, event: Event) -> None:
    """Write a reciprocal pair and keep pending result labels current.

    Partner repair is a pre-scoring operation. Pending EventResult rows mirror
    the repaired names for prints and scoring; completed rows are historical
    records and must not be rewritten by a repair pass.
    """
    # Store by event id and names to stay compatible with existing readers/imports.
    for key in [str(event.id), event.name, event.display_name]:
        a.set_partner(key, b.name)
        b.set_partner(key, a.name)

    EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=a.id,
        competitor_type=event.event_type,
        status='pending',
    ).update({'partner_name': b.name}, synchronize_session='fetch')
    EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=b.id,
        competitor_type=event.event_type,
        status='pending',
    ).update({'partner_name': a.name}, synchronize_session='fetch')


def _event_pool(event: Event) -> list:
    model = ProCompetitor if event.event_type == 'pro' else CollegeCompetitor
    competitors = (
        model.query.filter_by(
            tournament_id=event.tournament_id,
            status="active",
        )
        .order_by(model.name)
        .all()
    )

    if event.gender in {"M", "F"}:
        competitors = [c for c in competitors if c.gender == event.gender]

    return [c for c in competitors if _is_entered(event, c.get_events_entered())]


def _resolve_partner(partner_name: str, pool: list, exclude_id):
    """Fuzzy-resolve a partner name against the event pool.

    Wraps services.name_match.find_partner_match with the ProCompetitor
    name accessor and the comp's own id excluded so a typo'd partner
    that almost matches the comp's own name never resolves to themselves.
    """
    return find_partner_match(
        partner_name,
        pool,
        name_getter=lambda c: c.name,
        exclude_key=exclude_id,
    )


def _gender_pair_is_valid(event: Event, a, b) -> bool:
    requirement = getattr(event, 'partner_gender_requirement', None)
    return (
        requirement not in {'mixed', 'same'}
        or (requirement == 'mixed' and a.gender != b.gender)
        or (requirement == 'same' and a.gender == b.gender)
    )


def get_partner_repair_cases(event: Event) -> list[dict]:
    """Return current malformed pair declarations that require a decision.

    This is intentionally read-only. Automatic pairing handles only entrants
    with no inbound or outbound claim; every declaration that names somebody
    stays visible for an operator to repair or confirm.
    """
    if event.event_type not in {'pro', 'college'} or not event.is_partnered:
        return []

    pool = _event_pool(event)
    cases_by_id: dict[int, dict] = {}
    for competitor in pool:
        partner_name = _read_partner_name(competitor, event)
        if not partner_name:
            continue
        if normalize_alphanum(partner_name) == normalize_alphanum(competitor.name):
            cases_by_id[competitor.id] = {
                'competitor': competitor,
                'competitor_type': event.event_type,
                'partner_name': partner_name,
                'reason': 'self_reference',
                'suggested_partner': None,
            }
            continue

        partner = _resolve_partner(partner_name, pool, exclude_id=competitor.id)
        if partner is None:
            cases_by_id[competitor.id] = {
                'competitor': competitor,
                'competitor_type': event.event_type,
                'partner_name': partner_name,
                'reason': 'unresolved',
                'suggested_partner': None,
            }
            continue
        if not _gender_pair_is_valid(event, competitor, partner):
            cases_by_id[competitor.id] = {
                'competitor': competitor,
                'competitor_type': event.event_type,
                'partner_name': partner_name,
                'reason': 'invalid_gender',
                'suggested_partner': None,
            }
            continue

        their_partner_name = _read_partner_name(partner, event)
        their_match = (
            _resolve_partner(their_partner_name, pool, exclude_id=partner.id)
            if their_partner_name else None
        )
        if their_match is None or their_match.id != competitor.id:
            cases_by_id[competitor.id] = {
                'competitor': competitor,
                'competitor_type': event.event_type,
                'partner_name': partner_name,
                'reason': 'one_sided_claim' if not their_partner_name else 'non_reciprocal',
                'suggested_partner': partner if not their_partner_name else None,
            }

    return sorted(cases_by_id.values(), key=lambda case: case['competitor'].name.lower())


def get_unclaimed_partner_candidates(event: Event, exclude_ids: set[int] | None = None) -> list:
    """Return entrants safe to select manually without stealing a claim."""
    pool = _event_pool(event)
    excluded = exclude_ids or set()
    inbound_claims: set[int] = set()
    for competitor in pool:
        partner_name = _read_partner_name(competitor, event)
        if partner_name:
            partner = _resolve_partner(partner_name, pool, exclude_id=competitor.id)
            if partner is not None:
                inbound_claims.add(partner.id)

    return [
        competitor for competitor in pool
        if competitor.id not in excluded
        and competitor.id not in inbound_claims
        and not _read_partner_name(competitor, event)
    ]


def partner_claim_digest(event: Event, orphan, candidate) -> str:
    """Digest only the two competitors and claims that govern this repair."""
    pool = _event_pool(event)

    def state(competitor):
        inbound_claims = []
        for claimant in pool:
            partner_name = _read_partner_name(claimant, event)
            if not partner_name:
                continue
            resolved = _resolve_partner(partner_name, pool, exclude_id=claimant.id)
            if resolved is not None and resolved.id == competitor.id:
                inbound_claims.append((claimant.id, partner_name))
        entered_events = (
            competitor.get_events_entered()
            if hasattr(competitor, 'get_events_entered')
            else []
        )
        return {
            'id': competitor.id,
            'name': competitor.name,
            'status': competitor.status,
            'gender': competitor.gender,
            'entered': _is_entered(event, entered_events),
            'partner': _read_partner_name(competitor, event),
            'inbound_claims': sorted(inbound_claims),
        }

    payload = {
        'event': {
            'id': event.id,
            'status': event.status,
            'is_finalized': bool(event.is_finalized),
            'scoring_started': EventResult.query.filter_by(
                event_id=event.id,
                status='completed',
            ).first() is not None,
        },
        'orphan': state(orphan),
        'candidate': state(candidate),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def auto_assign_event_partners(event: Event) -> dict:
    """Resolve and auto-pair partners for one partnered event.

    Three-phase resolver:
      Phase 1 (CONFIRM): walk the pool, fuzzy-resolve each comp's partner
        field. When two comps fuzzy-resolve to each other, write the
        reciprocal partner JSON on both sides (idempotent — survives a
        re-run with no changes) and mark both as paired.
      Phase 2 (CLAIM): record one-sided claims. If A says B but B says
        someone else / blank, both A and B are flagged for operator
        review and removed from the auto-pair pool. The user explicitly
        called this out: "BE SURE to write something that checks if
        someone else has already claimed a partner before you throw them
        into the unpaired pool."
      Phase 3 (AUTO-PAIR): the genuinely unclaimed pool gets auto-paired
        with mixed-gender priority for mixed events, same-gender pairing
        otherwise. Truly unpairable competitors (odd count, or gender
        imbalance on a mixed event) are returned as ``unmatched`` so the
        operator can drop them with a flash notification.

    Returns:
        dict with keys ``event_id``, ``event``, ``confirmed_pairs``,
        ``assigned_pairs`` (NEW pairs created in phase 3),
        ``one_sided_claims`` (list of {competitor_id, claimed_partner_name,
        matched_partner_id_or_none}), ``unmatched`` (count of competitors
        the resolver could not pair anyone with).
    """
    summary = {
        "event_id": event.id,
        "event": event.display_name,
        "confirmed_pairs": 0,
        "assigned_pairs": 0,
        "one_sided_claims": [],
        "unmatched": 0,
    }
    if event.event_type not in {'pro', 'college'} or not event.is_partnered:
        return summary

    pool = _event_pool(event)
    if len(pool) < 2:
        return summary

    paired: set[int] = set()
    needs_review: set[int] = set()
    one_sided: list[dict] = []

    # ----- Phase 1: confirm or build reciprocal pairs ------------------
    for comp in pool:
        if comp.id in paired or comp.id in needs_review:
            continue
        partner_name = _read_partner_name(comp, event)
        if not partner_name:
            continue

        # Self-reference — operator typed their own name. Hold for review.
        if normalize_alphanum(partner_name) == normalize_alphanum(comp.name):
            needs_review.add(comp.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": None,
                    "reason": "self_reference",
                }
            )
            continue

        partner = _resolve_partner(partner_name, pool, exclude_id=comp.id)
        if partner is None or partner.id == comp.id:
            # Claimed name doesn't fuzzy-match anyone in the pool. Hold A
            # for review — do NOT throw them into the unclaimed pool, or
            # the auto-pair pass will mate them with someone unrelated.
            needs_review.add(comp.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": None,
                    "reason": "unresolved",
                }
            )
            continue
        if partner.id in paired:
            # Partner already locked in with someone else. Hold A for review.
            needs_review.add(comp.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": partner.id,
                    "reason": "partner_already_paired",
                }
            )
            continue

        # Check reciprocity: matched partner must fuzzy-resolve back to comp.
        their_partner_name = _read_partner_name(partner, event)
        if not their_partner_name:
            # B's partner field is blank — A claims B. We treat this as a
            # one-sided claim and DO NOT auto-confirm: race-day operators
            # have seen too many cases where A typo'd a name that fuzzy-
            # matched to someone unrelated. Hold both for review. The UI
            # offers a one-click "confirm A↔B" button.
            needs_review.add(comp.id)
            needs_review.add(partner.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": partner.id,
                    "matched_partner_name": partner.name,
                    "reason": "one_sided_claim",
                }
            )
            continue

        their_match = _resolve_partner(their_partner_name, pool, exclude_id=partner.id)
        if their_match is None or their_match.id != comp.id:
            # B claims someone else. Hold both for review; operator must
            # decide which side wins.
            needs_review.add(comp.id)
            needs_review.add(partner.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": partner.id,
                    "matched_partner_name": partner.name,
                    "partner_says": their_partner_name,
                    "reason": "non_reciprocal",
                }
            )
            continue

        # Reciprocal — write canonical names on both sides (heals typos).
        if (
            event.partner_gender_requirement == "mixed"
            and comp.gender == partner.gender
        ):
            # A reciprocal name match is not enough for Jack & Jill. Keeping
            # this pair would make an impossible heat after import succeeds.
            needs_review.add(comp.id)
            needs_review.add(partner.id)
            one_sided.append(
                {
                    "competitor_id": comp.id,
                    "competitor_name": comp.name,
                    "claimed_partner_name": partner_name,
                    "matched_partner_id": partner.id,
                    "matched_partner_name": partner.name,
                    "reason": "mixed_gender_required",
                }
            )
            continue

        set_partner_bidirectional(comp, partner, event)
        paired.add(comp.id)
        paired.add(partner.id)
        summary["confirmed_pairs"] += 1

    # ----- Phase 3: auto-pair the truly unclaimed ----------------------
    unclaimed = [c for c in pool if c.id not in paired and c.id not in needs_review]
    mixed_required = event.partner_gender_requirement == "mixed"
    if mixed_required:
        men = [c for c in unclaimed if c.gender == "M"]
        women = [c for c in unclaimed if c.gender == "F"]
        while men and women:
            a = men.pop(0)
            b = women.pop(0)
            set_partner_bidirectional(a, b, event)
            paired.add(a.id)
            paired.add(b.id)
            summary["assigned_pairs"] += 1
        leftover = men + women + [c for c in unclaimed if c.gender not in {"M", "F"}]
    else:
        leftover = list(unclaimed)
        while len(leftover) >= 2:
            a = leftover.pop(0)
            b = leftover.pop(0)
            set_partner_bidirectional(a, b, event)
            paired.add(a.id)
            paired.add(b.id)
            summary["assigned_pairs"] += 1

    summary["one_sided_claims"] = one_sided
    summary["unmatched"] = len(leftover) + len(needs_review)

    db.session.flush()
    return summary


def auto_assign_partners(tournament: Tournament) -> dict:
    """Auto assign partners across all partnered events in a tournament."""
    events = (
        tournament.events.filter_by(is_partnered=True)
        .order_by(Event.event_type, Event.name, Event.gender)
        .all()
    )
    summaries = [auto_assign_event_partners(event) for event in events]
    return {
        "event_count": len(summaries),
        "confirmed_pairs": sum(s["confirmed_pairs"] for s in summaries),
        "assigned_pairs": sum(s["assigned_pairs"] for s in summaries),
        "one_sided_claims": sum(len(s["one_sided_claims"]) for s in summaries),
        "unmatched": sum(s["unmatched"] for s in summaries),
        "events": summaries,
    }


def auto_assign_pro_partners(tournament: Tournament) -> dict:
    """Compatibility wrapper for the legacy pro-only registration action."""
    events = (
        tournament.events.filter_by(event_type="pro", is_partnered=True)
        .order_by(Event.name, Event.gender)
        .all()
    )
    summaries = [auto_assign_event_partners(event) for event in events]
    return {
        "event_count": len(summaries),
        "confirmed_pairs": sum(s["confirmed_pairs"] for s in summaries),
        "assigned_pairs": sum(s["assigned_pairs"] for s in summaries),
        "one_sided_claims": sum(len(s["one_sided_claims"]) for s in summaries),
        "unmatched": sum(s["unmatched"] for s in summaries),
        "events": summaries,
    }
