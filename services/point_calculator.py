"""
Point calculator service for college competition scoring.
Handles placement-based point awards and team aggregation.
"""
from database import db
from models import Event, EventResult, Team
from models.competitor import CollegeCompetitor
import config


def calculate_event_placements(event: Event) -> list:
    """
    Calculate placements for an event based on results.

    Handles different scoring types:
    - time: lowest wins
    - hits: lowest wins
    - score: highest wins
    - distance: highest wins

    Args:
        event: Event to calculate placements for

    Returns:
        List of EventResult objects with placements assigned
    """
    results = event.results.filter_by(status='completed').all()

    if not results:
        return []

    # Sort based on scoring type
    if event.scoring_type in ['time', 'hits']:
        # Lower is better
        results.sort(key=lambda r: r.result_value if r.result_value is not None else float('inf'))
    else:
        # Higher is better (score, distance)
        results.sort(key=lambda r: r.result_value if r.result_value is not None else 0, reverse=True)

    # Handle ties - competitors with same result get same position
    current_position = 1
    previous_value = None
    position_count = 0

    for result in results:
        if result.result_value != previous_value:
            current_position += position_count
            position_count = 1
        else:
            position_count += 1

        result.final_position = current_position
        previous_value = result.result_value

    db.session.commit()
    return results


def award_points(event: Event) -> dict:
    """
    Award points to competitors based on their placement.

    Points scheme: 1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1

    Args:
        event: Event to award points for

    Returns:
        Dict of competitor_id -> points awarded
    """
    if event.event_type != 'college':
        return {}

    results = event.results.filter(
        EventResult.final_position.isnot(None),
        EventResult.status == 'completed'
    ).all()

    points_awarded = {}

    for result in results:
        points = config.PLACEMENT_POINTS.get(result.final_position, 0)
        result.points_awarded = points
        points_awarded[result.competitor_id] = points

        # Update competitor's individual points
        competitor = CollegeCompetitor.query.get(result.competitor_id)
        if competitor:
            competitor.individual_points += points

            # Update team total
            if competitor.team:
                competitor.team.recalculate_points()

    db.session.commit()
    return points_awarded


def finalize_event_scoring(event: Event) -> dict:
    """
    Complete scoring workflow for an event:
    1. Calculate placements
    2. Award points (college) or payouts (pro)
    3. Update event status

    Args:
        event: Event to finalize

    Returns:
        Summary dict with placements and points/payouts
    """
    # Calculate placements
    results = calculate_event_placements(event)

    summary = {
        'event': event.display_name,
        'total_competitors': len(results),
        'placements': []
    }

    if event.event_type == 'college':
        # Award points
        points = award_points(event)

        for result in results:
            summary['placements'].append({
                'position': result.final_position,
                'name': result.competitor_name,
                'result': result.result_value,
                'points': result.points_awarded
            })
    else:
        # Award payouts
        payouts = event.get_payouts()

        for result in results:
            payout = float(payouts.get(str(result.final_position), 0))
            result.payout_amount = payout

            # Update competitor earnings
            from models.competitor import ProCompetitor
            competitor = ProCompetitor.query.get(result.competitor_id)
            if competitor:
                competitor.add_earnings(payout)

            summary['placements'].append({
                'position': result.final_position,
                'name': result.competitor_name,
                'result': result.result_value,
                'payout': payout
            })

        db.session.commit()

    # Mark event as completed
    event.status = 'completed'
    db.session.commit()

    return summary


def recalculate_all_team_points(tournament_id: int):
    """
    Recalculate all team points from member individual points.
    Useful after corrections or scratches.

    Args:
        tournament_id: Tournament to recalculate
    """
    teams = Team.query.filter_by(tournament_id=tournament_id).all()

    for team in teams:
        team.recalculate_points()

    db.session.commit()


def get_individual_standings(tournament_id: int, gender: str = None, limit: int = None) -> list:
    """
    Get individual standings sorted by points.

    Args:
        tournament_id: Tournament ID
        gender: Optional gender filter ('M' or 'F')
        limit: Optional limit on results

    Returns:
        List of (rank, competitor) tuples
    """
    query = CollegeCompetitor.query.filter_by(
        tournament_id=tournament_id,
        status='active'
    )

    if gender:
        query = query.filter_by(gender=gender)

    competitors = query.order_by(CollegeCompetitor.individual_points.desc()).all()

    if limit:
        competitors = competitors[:limit]

    # Calculate rankings (handle ties)
    standings = []
    current_rank = 1
    previous_points = None

    for i, comp in enumerate(competitors):
        if comp.individual_points != previous_points:
            current_rank = i + 1
        standings.append((current_rank, comp))
        previous_points = comp.individual_points

    return standings


def get_team_standings(tournament_id: int, limit: int = None) -> list:
    """
    Get team standings sorted by total points.

    Args:
        tournament_id: Tournament ID
        limit: Optional limit on results

    Returns:
        List of (rank, team) tuples
    """
    teams = Team.query.filter_by(
        tournament_id=tournament_id,
        status='active'
    ).order_by(Team.total_points.desc()).all()

    if limit:
        teams = teams[:limit]

    # Calculate rankings (handle ties)
    standings = []
    current_rank = 1
    previous_points = None

    for i, team in enumerate(teams):
        if team.total_points != previous_points:
            current_rank = i + 1
        standings.append((current_rank, team))
        previous_points = team.total_points

    return standings
