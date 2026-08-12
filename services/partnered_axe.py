"""
Partnered Axe Throw prelims/finals management.

The Partnered Axe Throw event has a unique format:
1. All pairs compete in prelims (hits-based scoring)
2. Top 4 pairs advance to finals
3. Finals determine final placements

Each pair consists of two competitors throwing at the same target,
alternating throws. Score is total hits combined.
"""
import json

from database import db
from models import Event, EventResult, Heat, HeatAssignment
from models.competitor import ProCompetitor


class PartneredAxeThrow:
    """Manages the Partnered Axe Throw prelims/finals flow."""

    def __init__(self, event: Event):
        self.event = event
        self._load_state()

    def _load_state(self) -> dict:
        """Load partnered axe state from event_state column with payouts fallback.

        Reads from event_state (primary) with fallback to payouts for
        backward compatibility during the migration transition.
        """
        raw = self.event.event_state
        if not raw:
            # Fallback: payouts column (legacy path)
            raw = self.event.payouts
        try:
            self.state = json.loads(raw or '{}')
        except (json.JSONDecodeError, TypeError):
            # Specific catch only — bare except masks KeyboardInterrupt and
            # SystemExit and hides unrelated failures during state load.
            self.state = {}

        if 'stage' not in self.state:
            self.state = {
                'stage': 'prelims',  # prelims, finals, completed
                'prelim_results': [],
                'finalists': [],
                'final_results': [],
                'pairs': []
            }

        return self.state

    def _save_state(self, commit: bool = True):
        """Save state to event.

        Args:
            commit: When True (default) commits immediately so existing
                callers are unaffected.  Pass commit=False to flush into
                the current session without committing so the caller can
                wrap multiple saves in a single outer transaction.
        """
        self.event.event_state = json.dumps(self.state)
        if commit:
            db.session.commit()

    def get_stage(self) -> str:
        """Get current stage: prelims, finals, or completed."""
        return self.state.get('stage', 'prelims')

    def get_pairs(self) -> list:
        """Get all registered pairs."""
        return self.state.get('pairs', [])

    def paired_competitor_ids(self) -> dict:
        """{competitor_id: pair_id} for everyone already standing in a pair.

        The dashboard uses this to stop offering people who are already
        registered, and register_pair uses it to refuse the submission if
        the dropdown is bypassed.
        """
        holders = {}
        for pair in self.state.get('pairs', []):
            for key in ('competitor1', 'competitor2'):
                member = pair.get(key) or {}
                cid = member.get('id')
                if cid is None:
                    continue
                try:
                    cid = int(cid)
                except (TypeError, ValueError):
                    continue
                holders.setdefault(cid, pair.get('pair_id'))
        return holders

    def register_pair(self, competitor1_id: int, competitor2_id: int) -> dict:
        """
        Register a pair for the event.

        Both competitors must belong to the same tournament as this
        Partnered Axe event, be active, and have Partnered Axe Throw in
        their events_entered list. Without these checks, a tampered POST
        could inject competitors from another tournament into this
        event's state JSON (tenancy leak) or pair competitors who never
        signed up for the event.

        Neither may already be standing in another pair. That check is
        not cosmetic: event_results holds at most one row per competitor
        per event (uq_event_result_competitor), so the two writers below
        take the update-in-place branch rather than raising. A competitor
        registered twice is published at whichever pair's score was
        written last, silently, while the partnered-axe standings page
        still shows both pairs at their own scores.

        Returns pair info dict.
        """
        # Filter tenancy at query time so a crafted ID from another
        # tournament fails the existence check, not just the comparison.
        comp1 = ProCompetitor.query.filter_by(
            id=competitor1_id,
            tournament_id=self.event.tournament_id,
            status='active',
        ).first()
        comp2 = ProCompetitor.query.filter_by(
            id=competitor2_id,
            tournament_id=self.event.tournament_id,
            status='active',
        ).first()

        if not comp1 or not comp2:
            raise ValueError("One or both competitors not found in this tournament")

        # Must be entered in Partnered Axe Throw. events_entered is a JSON
        # list of event IDs (int or str) — accept either form.
        event_id = self.event.id
        for comp, label in ((comp1, 'competitor 1'), (comp2, 'competitor 2')):
            entered = set()
            for raw in comp.get_events_entered():
                try:
                    entered.add(int(raw))
                except (TypeError, ValueError):
                    continue
            if event_id not in entered:
                raise ValueError(
                    f"{comp.name} ({label}) is not entered in Partnered Axe Throw"
                )

        # One competitor, one pair.
        if comp1.id == comp2.id:
            raise ValueError("A competitor cannot be paired with themselves")

        already = self.paired_competitor_ids()
        for comp in (comp1, comp2):
            if comp.id in already:
                raise ValueError(
                    f"{comp.name} is already registered in pair "
                    f"{already[comp.id]}. A competitor can only be in one pair."
                )

        pair = {
            'pair_id': len(self.state['pairs']) + 1,
            'competitor1': {
                'id': comp1.id,
                'name': comp1.name
            },
            'competitor2': {
                'id': comp2.id,
                'name': comp2.name
            },
            'prelim_score': None,
            'final_score': None,
            'final_position': None
        }

        self.state['pairs'].append(pair)
        self._save_state()

        return pair

    def record_prelim_result(self, pair_id: int, hits: int):
        """
        Record a pair's prelim result.

        Args:
            pair_id: The pair ID
            hits: Total hits scored by the pair

        Raises:
            ValueError: if the bracket has moved past prelims, or if no pair
                carries this id.
        """
        # Stage guard. This method ends by calling
        # _sync_prelim_to_event_results, which writes prelim_score straight
        # over EventResult.result_value. Once the bracket has been cut those
        # rows hold the FINALS score and the placing, published by
        # _save_event_results. final_position is not touched here, so the row
        # does not even revert cleanly to a prelim row: it comes out a hybrid,
        # first place holding whatever number was typed into the prelim box,
        # while event_state still carries the real finals score. The results
        # page and the scoring page then disagree with nothing to say which is
        # right.
        #
        # The guard is on the STAGE, not on "this pair already has a prelim
        # score". Both readings stop the corruption; only this one leaves the
        # operator able to correct a mis-heard hit count before the cut, which
        # is routine.
        #
        # Deliberately NOT mirrored onto record_final_result. Re-entering a
        # finals score after the bracket completes already works correctly
        # (it re-sorts the placings and republishes every row) and it is the
        # only in-app way to fix a mis-heard number on the deciding throw.
        # Blocking it would leave reset(), which wipes the pairs and the
        # prelims too, as the only way back.
        #
        # Same shape as the advance_to_finals guard below, for the same
        # reason: the route flashes success on whatever it gets back, so
        # refusing out loud beats returning quietly. reset() rebuilds the
        # stage from a literal, so resetting the event clears this guard.
        stage = self.get_stage()
        if stage != 'prelims':
            raise ValueError(
                f"Prelim scores can no longer be entered for this event "
                f"(stage: {stage}). The bracket has already been cut, and "
                f"this entry would overwrite a published result. Reset the "
                f"event if the bracket needs to be rebuilt."
            )

        recorded_pair = None
        for pair in self.state['pairs']:
            if pair['pair_id'] == pair_id:
                pair['prelim_score'] = hits
                recorded_pair = pair
                break

        # A pair_id that matches nothing used to fall out of this loop, leave
        # recorded_pair None, and return without raising, at which point the
        # route flashed "Prelim result recorded for Pair 999". Storing nothing
        # is the correct response to a pair that does not exist; announcing it
        # as recorded is not. At a live show that is a score the judge
        # believes is on the board.
        if recorded_pair is None:
            raise ValueError(
                f"No pair {pair_id} is registered for this event. Nothing was "
                f"recorded."
            )

        # Update prelim_results sorted by score (descending)
        self.state['prelim_results'] = sorted(
            [p for p in self.state['pairs'] if p['prelim_score'] is not None],
            key=lambda x: x['prelim_score'],
            reverse=True
        )

        self._save_state()

        # Cross-populate to EventResult records so scores appear in the
        # regular scoring view as well.
        if recorded_pair:
            self._sync_prelim_to_event_results(recorded_pair)

    def get_prelim_standings(self) -> list:
        """Get prelim standings sorted by score (highest first)."""
        pairs_with_scores = [p for p in self.state['pairs'] if p['prelim_score'] is not None]
        return sorted(pairs_with_scores, key=lambda x: x['prelim_score'], reverse=True)

    def can_advance_to_finals(self) -> bool:
        """Whether the Advance to Finals button should be offered.

        Prelims complete AND still in the prelims stage. The stage half is not
        cosmetic: prelims never become incomplete again, so without it this
        stays True for the rest of the event and the button stays live on every
        page that asks. templates/partnered_axe/prelims.html already renders the
        button behind ``can_advance and stage == 'prelims'``, so the stage test
        belonged in here from the start; the template was compensating for its
        absence, and routes/partnered_axe.py:299 (the api/status endpoint) was
        not.
        """
        if self.get_stage() != 'prelims':
            return False
        scored = [p for p in self.state['pairs'] if p['prelim_score'] is not None]
        return len(scored) >= 4 and len(scored) == len(self.state['pairs'])

    def advance_to_finals(self) -> list:
        """
        Advance top 4 pairs to finals.

        Returns list of finalist pairs.
        """
        # Idempotency guard. This method reseeds state['finalists'] from the
        # prelim standings, and the pair dicts in those standings carry
        # final_score None, so a second call destroys every finals score
        # already entered. record_final_result only writes finals scores back
        # into state['pairs'] once ALL FOUR are in, so a bracket scored halfway
        # keeps those scores nowhere else and loses them outright.
        #
        # After all four are in, the damage is worse: stage is 'completed',
        # final_position is assigned, and _save_event_results has already
        # published EventResult rows. Reseeding drops the placings but not the
        # published rows, so the results page and the event state disagree with
        # no indication which is right.
        #
        # Refuse rather than quietly return the existing finalists: the route
        # flashes success on whatever it gets back, and an operator who is told
        # the press worked stops looking for the problem. The route already
        # turns ValueError into a danger flash.
        #
        # The check reads the stage out of self.state, which reset() rebuilds
        # from a literal, so resetting the event clears the guard along with
        # everything else and the bracket can be run again.
        stage = self.get_stage()
        if stage != 'prelims':
            raise ValueError(
                f"Finals have already been seeded for this event (stage: "
                f"{stage}). Advancing again would clear the finals scores. "
                f"Reset the event if the bracket needs to be rebuilt."
            )

        if not self.can_advance_to_finals():
            raise ValueError("Cannot advance to finals - not all prelim results recorded")

        standings = self.get_prelim_standings()
        self.state['finalists'] = standings[:4]
        self.state['stage'] = 'finals'
        self._save_state()

        return self.state['finalists']

    def get_finalists(self) -> list:
        """Get the finalist pairs."""
        return self.state.get('finalists', [])

    def record_final_result(self, pair_id: int, hits: int):
        """
        Record a pair's final result.

        Args:
            pair_id: The pair ID
            hits: Total hits scored in finals

        Raises:
            ValueError: if no finalist carries this id.

        There is no stage guard here, and that asymmetry with
        record_prelim_result is deliberate. See the note in that method.
        """
        recorded_pair = None
        for pair in self.state['finalists']:
            if pair['pair_id'] == pair_id:
                pair['final_score'] = hits
                recorded_pair = pair
                break

        # Same silent no-op as the prelim writer had, on the side where the
        # number decides the placings.
        if recorded_pair is None:
            raise ValueError(
                f"Pair {pair_id} is not in the finals bracket for this event. "
                f"Nothing was recorded."
            )

        # Check if all finals complete
        all_scored = all(p.get('final_score') is not None for p in self.state['finalists'])

        if all_scored:
            # Sort by final score and assign positions
            sorted_finals = sorted(
                self.state['finalists'],
                key=lambda x: x['final_score'],
                reverse=True
            )

            for position, pair in enumerate(sorted_finals, 1):
                pair['final_position'] = position
                # Update in main pairs list too
                for main_pair in self.state['pairs']:
                    if main_pair['pair_id'] == pair['pair_id']:
                        main_pair['final_position'] = position
                        main_pair['final_score'] = pair['final_score']

            self.state['finalists'] = sorted_finals
            self.state['final_results'] = sorted_finals
            self.state['stage'] = 'completed'

            # Final state, result rows, positions, payouts, and earnings must
            # publish together.  A split commit can leave the standings page
            # claiming a winner while the payout ledger still reflects the
            # prelims or a prior finals correction.
            try:
                self._save_event_results(commit=False)
                self._save_state(commit=False)
                db.session.flush()

                # The scoring engine knows that this state machine owns the
                # finishing order and reads it instead of mixing prelim and
                # finals scores from result_value.
                from services.scoring_engine import calculate_positions
                calculate_positions(self.event)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            return

        self._save_state()

    def _sync_prelim_to_event_results(self, pair: dict):
        """Sync a single pair's prelim score to EventResult records.

        Creates or updates one EventResult per competitor in the pair so that
        prelim scores are visible from the regular scoring/event-results page.
        """
        for comp_key in ('competitor1', 'competitor2'):
            comp = pair[comp_key]
            partner_key = 'competitor2' if comp_key == 'competitor1' else 'competitor1'
            partner = pair[partner_key]

            existing = EventResult.query.filter_by(
                event_id=self.event.id,
                competitor_id=comp['id'],
                competitor_type='pro',
            ).first()

            if existing:
                existing.result_value = pair['prelim_score']
                existing.partner_name = partner['name']
                existing.status = 'completed'
            else:
                result = EventResult(
                    event_id=self.event.id,
                    competitor_type='pro',
                    competitor_id=comp['id'],
                    competitor_name=comp['name'],
                    partner_name=partner['name'],
                    result_value=pair['prelim_score'],
                    status='completed',
                )
                db.session.add(result)

        db.session.commit()

    def _save_event_results(self, commit: bool = True):
        """Save final results to EventResult table.

        Updates existing records (created during prelim cross-populate) when
        present, otherwise creates new ones.
        """
        for pair in self.state['finalists']:
            for competitor_key in ['competitor1', 'competitor2']:
                competitor = pair[competitor_key]

                existing = EventResult.query.filter_by(
                    event_id=self.event.id,
                    competitor_id=competitor['id'],
                    competitor_type='pro',
                ).first()

                if existing:
                    existing.result_value = pair['final_score']
                    existing.final_position = pair['final_position']
                    existing.competitor_name = competitor['name']
                    existing.partner_name = pair[
                        'competitor2' if competitor_key == 'competitor1'
                        else 'competitor1'
                    ]['name']
                    existing.status = 'completed'
                else:
                    partner = pair[
                        'competitor2' if competitor_key == 'competitor1'
                        else 'competitor1'
                    ]
                    result = EventResult(
                        event_id=self.event.id,
                        competitor_type='pro',
                        competitor_id=competitor['id'],
                        competitor_name=competitor['name'],
                        partner_name=partner['name'],
                        result_value=pair['final_score'],
                        final_position=pair['final_position'],
                        status='completed',
                    )
                    db.session.add(result)

        if commit:
            db.session.commit()

    def get_final_standings(self) -> list:
        """Get final standings (only available after finals complete)."""
        if self.state['stage'] != 'completed':
            return []
        return self.state.get('final_results', [])

    def get_full_standings(self) -> list:
        """
        Get full standings combining prelims and finals.

        Returns:
            List of all pairs with their positions:
            - 1st-4th from finals
            - 5th+ from prelim standings
        """
        finalists_ids = {p['pair_id'] for p in self.state.get('finalists', [])}
        prelim_standings = self.get_prelim_standings()

        # Start with final results
        results = list(self.state.get('final_results', []))

        # Add non-finalists from prelims
        position = 5
        for pair in prelim_standings:
            if pair['pair_id'] not in finalists_ids:
                pair_copy = dict(pair)
                pair_copy['final_position'] = position
                results.append(pair_copy)
                position += 1

        return results

    def reset(self):
        """Reset the event to initial state without leaving stale results.

        A reset deliberately discards the current bracket.  It therefore also
        removes its EventResult ledger rows and reverses their cached pro
        earnings.  Paid entries are a financial record, so they must be
        explicitly reopened by an administrator before the bracket can be
        reset.
        """
        results = EventResult.query.filter_by(event_id=self.event.id).all()
        if any(result.payout_settled for result in results):
            raise ValueError(
                'Cannot reset Partnered Axe Throw while payouts are settled. '
                'Reopen the settled payouts first.'
            )

        try:
            for result in results:
                payout = float(result.payout_amount or 0.0)
                if payout and result.competitor_type == 'pro':
                    competitor = db.session.get(ProCompetitor, result.competitor_id)
                    if competitor:
                        competitor.total_earnings = max(
                            0.0, float(competitor.total_earnings or 0.0) - payout
                        )
                db.session.delete(result)

            self.event.status = 'pending'
            self.event.is_finalized = False
            self.state = {
                'stage': 'prelims',
                'prelim_results': [],
                'finalists': [],
                'final_results': [],
                'pairs': []
            }
            self._save_state(commit=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        self.state = {
            'stage': 'prelims',
            'prelim_results': [],
            'finalists': [],
            'final_results': [],
            'pairs': []
        }
        self._save_state()


def find_partnered_axe_throw(tournament_id: int) -> PartneredAxeThrow | None:
    """Return the Partnered Axe Throw manager for a tournament, or None.

    Read-only: never creates an Event row. Callers that render a GET page
    MUST use this function, not ``get_partnered_axe_throw``. Auto-creating
    on GET would plant a phantom Event in tournaments that never configured
    Partnered Axe — the same GET-with-side-effect class of bug that filled
    the Woodboss config table with ghost rows.
    """
    event = Event.query.filter_by(
        tournament_id=tournament_id,
        name='Partnered Axe Throw',
    ).first()
    return PartneredAxeThrow(event) if event else None


def get_or_create_partnered_axe_throw(tournament_id: int) -> PartneredAxeThrow:
    """Return or create the Partnered Axe Throw manager for a tournament.

    Write-allowed: creates the Event row on first call. Reserved for POST
    handlers that represent an explicit decision to enable the event
    (register-pair, record-prelim, etc.) — never use on GET.
    """
    pat = find_partnered_axe_throw(tournament_id)
    if pat is not None:
        return pat

    event = Event(
        tournament_id=tournament_id,
        name='Partnered Axe Throw',
        event_type='pro',
        scoring_type='hits',
        is_partnered=True,
        status='pending',
    )
    db.session.add(event)
    db.session.commit()
    return PartneredAxeThrow(event)


# Back-compat alias. Prefer the explicit names above.
def get_partnered_axe_throw(tournament_id: int) -> PartneredAxeThrow:
    """Deprecated. Use find_partnered_axe_throw or get_or_create_partnered_axe_throw."""
    return get_or_create_partnered_axe_throw(tournament_id)
