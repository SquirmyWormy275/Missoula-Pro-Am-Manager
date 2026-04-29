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

import re

from database import db
from models import Event, Tournament
from models.competitor import ProCompetitor
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


def _read_partner_name(comp: ProCompetitor, event: Event) -> str:
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


def _set_partner_bidirectional(
    a: ProCompetitor, b: ProCompetitor, event: Event
) -> None:
    # Store by event id and names to stay compatible with existing readers/imports.
    for key in [str(event.id), event.name, event.display_name]:
        a.set_partner(key, b.name)
        b.set_partner(key, a.name)


def _event_pool(event: Event) -> list[ProCompetitor]:
    competitors = (
        ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status="active",
        )
        .order_by(ProCompetitor.name)
        .all()
    )

    if event.gender in {"M", "F"}:
        competitors = [c for c in competitors if c.gender == event.gender]

    return [c for c in competitors if _is_entered(event, c.get_events_entered())]


def _resolve_partner(partner_name: str, pool: list, exclude_id) -> ProCompetitor | None:
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


def auto_assign_event_partners(event: Event) -> dict:
    """Resolve and auto-pair partners for one partnered pro event.

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
    if event.event_type != "pro" or not event.is_partnered:
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
        _set_partner_bidirectional(comp, partner, event)
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
            _set_partner_bidirectional(a, b, event)
            paired.add(a.id)
            paired.add(b.id)
            summary["assigned_pairs"] += 1
        leftover = men + women + [c for c in unclaimed if c.gender not in {"M", "F"}]
    else:
        leftover = list(unclaimed)
        while len(leftover) >= 2:
            a = leftover.pop(0)
            b = leftover.pop(0)
            _set_partner_bidirectional(a, b, event)
            paired.add(a.id)
            paired.add(b.id)
            summary["assigned_pairs"] += 1

    summary["one_sided_claims"] = one_sided
    summary["unmatched"] = len(leftover) + len(needs_review)

    db.session.flush()
    return summary


def auto_assign_pro_partners(tournament: Tournament) -> dict:
    """Auto assign partners across all partnered pro events in a tournament."""
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
