# 2027 Owner Decision Packet

Status: **NOT APPROVED**

This packet records the annual business choices that code and test evidence
cannot make. Completing a field does not change the system by itself. The
approved packet must be reconciled into `docs/DOMAIN_CONTRACT.md`, tournament
configuration, tests, and the release checklist before 2027 race-day
certification can be claimed.

Owner: Alex Kaper

Approval date: ____________________

Approved configuration revision or commit: ____________________

## 1. Event Window And Recovery Objectives

All times use `America/Denver` unless the owner records a different zone.

| Decision | Owner selection |
|---|---|
| Friday competition date and operating window | ____________________ |
| Saturday competition date and operating window | ____________________ |
| Friday Night Feature window, if used | ____________________ |
| Backup high-frequency window | ____________________ |
| Maximum acceptable data loss during live competition (RPO) | ____________________ |
| Maximum acceptable recovery time during live competition (RTO) | ____________________ |
| Maximum acceptable data loss outside the live window | ____________________ |
| Maximum acceptable recovery time outside the live window | ____________________ |

Recommended conservative baseline: daily encrypted backups outside the event
window, hourly encrypted backups from 24 hours before the first competition
window until 24 hours after the last window, live local/offline operator
continuity, and an owner-approved one-hour RPO and one-hour RTO for hosted
recovery. Approving a target does not prove it; the exact cadence and a timed
restore rehearsal must still meet it.

## 2. College Event Matrix

For each traditionally open event, select whether it is OPEN or CLOSED in
2027. A CLOSED selection counts against the six-event athlete limit.

| Event | OPEN | CLOSED | Field limit or notes |
|---|:---:|:---:|---|
| Axe Throw | [ ] | [ ] | ____________________ |
| Peavey Log Roll | [ ] | [ ] | ____________________ |
| Caber Toss | [ ] | [ ] | ____________________ |
| Pulp Toss | [ ] | [ ] | ____________________ |

Select each scheduled closed event and record any field limit. Unchecked means
not offered in 2027.

| Event | Include | Field limit or notes |
|---|:---:|---|
| Underhand Hard Hit, men and women | [ ] | ____________________ |
| Underhand Speed, men and women | [ ] | ____________________ |
| Standing Block Hard Hit, men and women | [ ] | ____________________ |
| Standing Block Speed, men and women | [ ] | ____________________ |
| Single Buck, men and women | [ ] | ____________________ |
| Double Buck, men and women | [ ] | ____________________ |
| Jack and Jill Sawing | [ ] | ____________________ |
| Stock Saw, men and women | [ ] | ____________________ |
| Speed Climb, men and women | [ ] | ____________________ |
| Obstacle Pole, men and women | [ ] | ____________________ |
| Chokerman's Race, men and women | [ ] | ____________________ |
| College Birling, separate men and women | [ ] | ____________________ |
| 1-Board Springboard, men and women | [ ] | ____________________ |

College closed-event cap for 2027: ______ events per athlete.

Recommended selection: retain the owner-authored maximum of six unless the
event matrix and show-duration rehearsal prove a different cap is required.

## 3. Pro Event Matrix

Pro events are selected annually based on wood and field availability. Check
only the variants that will be offered.

| Event | Include/variants | Field limit or notes |
|---|---|---|
| Springboard | ____________________ | ____________________ |
| Pro 1-Board | ____________________ | ____________________ |
| 3-Board Jigger | ____________________ | ____________________ |
| Underhand | ____________________ | ____________________ |
| Standing Block | ____________________ | ____________________ |
| Stock Saw | ____________________ | ____________________ |
| Hot Saw | ____________________ | ____________________ |
| Single Buck | ____________________ | ____________________ |
| Double Buck | ____________________ | ____________________ |
| Jack and Jill | ____________________ | ____________________ |
| Partnered Axe Throw | ____________________ | ____________________ |
| Obstacle Pole | ____________________ | ____________________ |
| Cookie Stack | ____________________ | ____________________ |
| Pole Climb | ____________________ | ____________________ |

Friday Night Feature selections: ____________________

Saturday college spillover selections, in priority order: ____________________

Mandatory baseline unless expressly changed: College Chokerman Run 2 closes
Saturday spillover; Pro Obstacle Pole has one run; College Obstacle Pole has
two runs.

## 4. Springboard Resource Policy

Current contract behavior is fail-closed: left-handed cutters use the
configured left-handed dummy, are spread across heats, and generation expands
the heat count until no heat needs that dummy more than once. Existing owner
requirements also state that four dummies may each be used three times, which
needs an explicit 2027 interpretation.

Select one:

- [ ] **Keep fail-closed expansion (recommended).** Add heats as needed and
  reject generation if the configured stand set cannot represent the field.
- [ ] **Authorize a bounded exception.** State the maximum uses per dummy,
  required crew acknowledgment, and the exact condition under which the
  operator may proceed: ________________________________________________
- [ ] **Use another rule:** _____________________________________________

Approved left-handed dummy stand number: ______

Approved maximum uses per dummy for each Springboard event: ______

## 5. Birling

College Birling is currently modeled as separate men's and women's
double-elimination brackets. The owner-authored pro rules say Pro Birling is
not gender-separated and, when held, is last; the current pro event list does
not enable it by default.

| Decision | Owner selection |
|---|---|
| Hold College Birling in 2027? | Yes / No |
| College Birling remains last on Friday? | Yes / No |
| Hold Pro Birling in 2027? | Yes / No |
| If held, Pro Birling is one mixed bracket? | Yes / No |
| If held, Pro Birling is the final pro event before mandatory spillover? | Yes / No |
| Bracket format and places awarded | ____________________ |

Recommended baseline: retain gender-separated College Birling as a pre-seeded
double-elimination bracket through sixth place; leave Pro Birling disabled
unless its field, bracket format, and Saturday closing-order impact are
explicitly approved and rehearsed.

## 6. Pro-Am Relay

The following owner-authored rules are treated as fixed unless explicitly
amended here: each team has exactly eight competitors, with two Professional
Men, two Professional Women, two College Men, and two College Women; event
order is Partnered Sawing, Standing Butcher Block, Underhand Butcher Block,
then Team Axe Throw; the fee is $5 whether or not the entrant is drawn;
participation is not guaranteed; and Relay results do not affect College team
or individual points.

| Annual decision | Owner selection |
|---|---|
| Enable the Relay in 2027? | Yes / No |
| Number of teams, or rule for deriving it | ____________________ |
| Any eligibility exclusions | ____________________ |
| One Relay flight or another show placement | ____________________ |
| Prize money and payout places | ____________________ |
| Lottery draw deadline and approving operator | ____________________ |

Recommended baseline: derive the maximum number of complete teams from the
smallest eligible cohort, draw only complete eight-person teams, place the
Relay in the final normal pro flight before Saturday college spillover, and
never imply that paying the fee guarantees a draw.

## 7. Ties And Two-Timer Scoring

Current code averages two valid timer readings and leaves a one-timer row
partial. Tie splitting exists in the scoring surface, but the repository lacks
a strong dated owner authorization for the 2027 policy.

| Decision | Owner selection |
|---|---|
| Use the arithmetic mean of two timers? | Yes / No |
| Rounding precision and displayed precision | ____________________ |
| One missing/invalid timer blocks completion? | Yes / No |
| Exact tie in College points events | Split / Throw-off / Other: __________ |
| Exact tie in Pro payout events | Split / Throw-off / Other: __________ |
| Events that always require a throw-off or event-specific tiebreak | ____________________ |
| Payout remainder/rounding rule after a split | ____________________ |

Recommended baseline: require both timers, use their arithmetic mean at the
system's retained precision, and require an explicit event-specific throw-off
where the event rules define one. Approve the split and rounding rule before
money or College points are published.

## 8. Backup Custody, Retention, And Audit

The hosted workflow remains scheduled but fails closed until all required
values and custody roles are approved. The recovery private identity must not
be stored in GitHub, Railway, the repository, or the application environment.

| Decision | Owner selection |
|---|---|
| Read-only production dump-role administrator | ____________________ |
| `age` public recipient approver | ____________________ |
| Separate private-key custodian | ____________________ |
| Backup workflow/audit owner | ____________________ |
| Encrypted artifact retention | ______ days |
| Failed/obsolete artifact deletion owner | ____________________ |
| Restore rehearsal operator | ____________________ |
| Restore rehearsal cadence | ____________________ |
| Download/access review cadence | ____________________ |

Recommended baseline: keep the current 90-day encrypted-artifact retention,
use separate people or separately controlled accounts for workflow
administration and private-key custody, rehearse a retained-artifact restore
before the event window and after any backup-workflow change, and review every
artifact download.

## 9. Final Owner Disposition

- [ ] Approved exactly as completed above.
- [ ] Approved with these exceptions: __________________________________
- [ ] Not approved; return for revision.

Owner signature or recorded approval reference: ____________________

Implementation reviewer: ____________________

Certification evidence reviewer: ____________________

No unchecked or blank decision is implied by this packet. This packet alone
does not determine deployability and cannot grant 2027 race-day certification.
