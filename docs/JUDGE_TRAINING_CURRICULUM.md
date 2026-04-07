# Judge Training Curriculum
## Missoula Pro-Am Manager — From Clipboard to Tablet

**Audience:** A judge who has worked the Missoula Pro Am for years and knows the events, the rules, the athletes, and the flow of the show — but has never used a tournament management app, and may not be comfortable with web software in general.

**Goal:** By the end of this curriculum, you will be able to log into the app, find any heat or competitor in under 30 seconds, enter scores correctly, fix mistakes, finalize events, and answer competitor questions — without help.

**Format:** 8 modules. Each module has a *purpose*, a *walkthrough*, a *practice drill*, and a *gotchas* list. Run them in order. Plan on roughly 3–4 hours total to go through everything once, plus a dry-run on a demo tournament.

---

## How to Use This Document

1. Sit down at a laptop or tablet with the app running on a **demo tournament** — never your first time on a live show. Ask the system administrator to load demo data or clone last year's tournament.
2. Read each module's *Purpose* first so you know **why** you're about to click things.
3. Do the *Walkthrough* with the app open in front of you. Click every button mentioned, even if it feels redundant.
4. Do the *Practice Drill* without looking at the walkthrough. If you get stuck, that's the spot you need to re-read.
5. Read the *Gotchas* twice. They are the things that have actually broken on real shows.

> **Rule of thumb:** If you're not sure what a button does, hover over it for a tooltip, or click it. The app is designed so almost nothing is permanently destructive without a confirmation dialog.

---

## Module 0 — Mental Model: What This App Replaces

### Purpose
Before you touch a single button, understand what the app *is* and what it *isn't*.

### What it replaces
- **The clipboard** with heat sheets and scratched-out times
- **The whiteboard** in the scorer's tent showing standings
- **The Excel files** that the team scorer used to maintain
- **The phone calls** between the head judge, the announcer, and the scoring tent ("what's the time on heat 4?")
- **The envelopes of cash** at the end of the day with hand-tallied payouts

### What it does NOT replace
- **Your eyes and your stopwatch.** The app does not time events. A human judge times each run with a stopwatch (or the official clock) and **types** the time into the app.
- **Your judgment on a fault.** The app does not see fouls, false starts, or incomplete cuts. You decide; the app records your decision.
- **The head judge's authority.** The app is a record-keeping tool. The head judge runs the show.

### Key vocabulary

| Term | What it means in the app |
|---|---|
| **Tournament** | One year of the Missoula Pro Am — typically Friday (college) + Saturday (pro). |
| **Event** | One competition: e.g. "Men's Underhand Chop", "Women's Single Buck". |
| **Heat** | One group of competitors going at the same time, on the stands assigned to that event. |
| **Flight** | A *block* of heats from different events shown back-to-back during the pro show on Saturday. Mixing keeps the crowd engaged and gives competitors rest. |
| **Stand** | A physical station — a chopping dummy, a pole, a sawing horse. |
| **Result** | One row of data: which competitor, which run, what time/score, what position. |
| **Finalize** | The act of locking an event after all heats are scored. Awards points and payouts. |
| **Scratch** | A competitor pulled before the event runs. Marked but not scored. |
| **DNF** | "Did Not Finish" — competitor started but did not complete. Recorded; counted in placement (last). |

### Gotchas
- The app uses **two divisions on different days**: Friday is **college**, Saturday is **pro**. Many things look similar across both, but the rules differ. Always check which division you are scoring.
- **Pro Birling does not exist** in this app. College Birling does (men's and women's brackets).

---

## Module 1 — Logging In and Finding Your Way Around

### Purpose
Get logged in, get oriented, and identify the four screens you will live in all weekend: the **Tournament Detail page**, the **Sidebar**, the **Show Day dashboard**, and the **Score Entry** screen.

### Walkthrough

1. **Open the app.** Someone (admin or scorekeeper) will have started the program and given you a URL — usually `http://localhost:5000` if running on a laptop in the scorer's tent, or the Railway URL if running off the cloud.
2. **Click "Judge"** on the role selection screen. This is the judge/admin/management entry point.
3. **Log in.** The admin will have created your account and given you a username and password. Pick a password you'll remember — there's no "forgot password" link. If you forget it, the admin has to reset it for you.
4. **You'll land on the dashboard.** The dashboard lists every tournament. Find this year's tournament and click it.
5. **You're now on the Tournament Detail page.** This is "home base." From here you can reach everything. Note the three sections:
   - **Before Show** — setup actions (registration, event config, heat generation)
   - **Game Day** — the actions you'll use during the show (score entry, show day dashboard)
   - **After Show** — finalization, payout settlement, exports
6. **Open the sidebar.** On the left, there's a collapsible sidebar with five sections:
   - **Show Entries** (teams, competitors, registration)
   - **Show Configuration** (events, wood specs, tournament settings)
   - **Scoring** (heat sheets, score entry, finalize)
   - **Results** (standings, all results, payouts)
   - **Admin** (users, audit log)
7. **Click "Show Day Dashboard"** under Scoring. This is where you'll spend most of your time during the show. It auto-refreshes every 60 seconds and shows live flights, current heat, and upcoming heats.

### Practice drill
Without looking back at the walkthrough:
1. Log out (top-right user menu).
2. Log back in.
3. Get from the dashboard to the Show Day Dashboard for this year's tournament in under 30 seconds.
4. From the sidebar, find "Score a Heat" and "View Standings."

### Gotchas
- The **back button works** in this app. You can always click your browser's back arrow.
- If the page looks frozen, **refresh it** (F5 or Ctrl+R). The app does not lose your unsaved work as long as you haven't clicked Save — but unsaved score entries DO get lost on refresh, so save first.
- The sidebar has an unscored-heats badge. **A red number means heats are waiting for scores.** Your job is to make that number go down.

---

## Module 2 — Reading a Heat Sheet on the App

### Purpose
Understand how the app organizes heats so that you can find any competitor in seconds. The Heat Sheet view replaces the paper heat sheet you've used for years.

### Walkthrough

1. From the sidebar, click **Heat Sheets** under Scoring. (Or: Tournament Detail → Show Day Dashboard → click any flight.)
2. You'll see all events for the selected day. Click an event — for example "Men's Underhand Chop."
3. The app shows every heat for that event, in order. Each heat shows:
   - **Heat number** (Heat 1, Heat 2, ...)
   - **Run number** (1 or 2 — relevant for two-run events like Speed Climb and Chokerman's Race)
   - **Competitors** with their assigned stand number
   - **Status**: Pending (not yet scored), In Progress, Completed
4. Click a heat to see the full detail.
5. Look at the top of the page — there's a button called **"Print Heat Sheet"** and **"Save as PDF"**. Use these if you want a paper backup.
6. **Color coding:** Each stand assignment gets a distinct color so a single glance tells you who's on stand 1 vs stand 2 vs stand 3. Pro events have a blue band; college events have a mauve band.

### Practice drill
1. Find the third heat of the Women's Single Buck event.
2. Identify which competitor is on stand 2 of that heat.
3. Print or save that heat sheet to PDF.
4. Find any heat that has a **gear-sharing warning badge** (yellow icon) and read what it says.

### Gotchas
- **Stand numbers matter.** When you call a heat, you call competitors to specific stands. The app's stand number must match the physical station. If you swap two competitors between stands, edit the heat in the app **before** they cut.
- **Two-run events show TWO heats per group of competitors** — one for Run 1, one for Run 2. They are separate heats in the app even though it's the same people. The app uses **best run** as their final time.
- A heat marked "Locked by Joe" means another judge or scorer is currently editing that heat. Do not double-enter scores. Wait, or coordinate over the radio.

---

## Module 3 — Entering Scores: The Daily Bread

### Purpose
This is the most important module. Score entry is what you'll do hundreds of times over two days. Get this right and the rest is easy.

### Walkthrough

1. From the Show Day Dashboard, click the current heat (or any heat marked "In Progress" or "Pending" that just finished).
2. Click **"Enter Results."**
3. You'll see a row for each competitor. Each row has:
   - **Competitor name** (read-only)
   - **Stand number** (read-only — this is where they cut/cut from)
   - **Result field** (time in seconds, or hits, or distance, depending on the event)
   - **Status dropdown** — leave as "Completed" unless they DNF'd or scratched
4. **Type the time exactly.** For chopping/sawing/climbing, time is in **seconds with decimals**: e.g. `14.83`. The app does NOT auto-format minutes. If a time is 1 minute 14.83 seconds, enter `74.83`.
5. **Tab between fields.** The Tab key jumps to the next competitor's result field. Faster than clicking.
6. **Click Save Results** at the bottom when all competitors in the heat are entered.
7. The heat now shows as **Completed** in green. The Show Day Dashboard updates automatically.

### Two-run events (Speed Climb, Chokerman's Race)
- Enter Run 1 results when Run 1 heats finish.
- Enter Run 2 results when Run 2 heats finish.
- The app **automatically uses the best (lowest) run** for placement. You don't have to do anything.

### Triple-run events
- Same workflow but with three runs. The app sums all three for placement (cumulative scoring).

### Hard Hit (axe throw, etc.)
- Enter **hits** as integers (e.g., `4`).
- Higher = better. Tiebreaks fall back to a throwoff (see Module 5).

### DNF and Scratch
- **DNF**: change Status to "DNF" — competitor started but did not finish. They count for placement (always last).
- **Scratched**: change Status to "Scratched" — they pulled out before the heat. Not counted.
- You can leave the result field blank for DNF/Scratched.

### Practice drill
1. Find a pending heat in the demo tournament.
2. Enter realistic times for every competitor (e.g., `15.2`, `16.7`, `18.3`, ...).
3. Mark one competitor as DNF.
4. Save. Verify the heat shows as Completed.
5. Open the same heat again and **change one time** (you made a typo). Save again. Verify the change took.

### Gotchas
- **Decimals only** — never enter `1:14.83`. Always convert to total seconds.
- **Save before leaving the page.** If you click a sidebar link or hit the back button before saving, your typed scores are lost.
- **The Save button shows a spinner briefly** — wait for it to finish before clicking again. Double-clicking can sometimes save twice.
- If you see a warning **"Another judge edited this heat — reload to see the latest"**, that means someone else updated this heat while you were typing. **Reload the page** and re-enter only the new scores.
- If you enter a wildly different time from the rest of the heat (e.g., `1.5` when everyone else is `15+`), the app flags it with a warning icon. **Double-check** before saving — most of the time you forgot a digit.

---

## Module 4 — Fixing Mistakes

### Purpose
You will make mistakes. Fixing them is easy if you know the workflow.

### Walkthrough

#### Wrong time entered
1. Go back to the heat (Heat Sheets → event → heat).
2. Click "Enter Results" again.
3. Change the value, click Save.
4. If the event has been **finalized**, you have one extra step: an admin must un-finalize it first. Flag the head judge.

#### Wrong competitor in a heat
1. From the event's heat list, click "Edit Heat."
2. Move the competitor to the correct heat. The app handles stand reassignment.
3. Save.

#### A competitor needs to scratch mid-day
1. From Show Entries → competitor list, find them.
2. Click their name → "Scratch Competitor."
3. They will be removed from all future heats automatically. Heats they were already in show them as scratched.

#### Two competitors got swapped on the stands
- If they cut and you typed the times against the wrong names: open the heat, **swap the times** between the two rows manually, save. Faster than trying to swap competitors.

### Practice drill
1. Enter scores for a heat. Save.
2. Change one score. Save.
3. Move a competitor from one heat to another (use Edit Heat).
4. Scratch a competitor. Verify they no longer appear in any future heat for any event.

### Gotchas
- **Editing a finalized event** is locked by default. This protects payouts. Get the head judge to un-finalize the event before editing — and re-finalize after.
- **The audit log records every change.** Don't worry about "covering tracks" — the app has your back, but it also has a memory. Always tell the head judge when you make a correction.
- **If you scratch the wrong competitor**, you can un-scratch them: go back to their detail page and click "Reactivate" or change their status back to active.

---

## Module 5 — Ties, Throwoffs, and Finalizing an Event

### Purpose
Understand what happens when the math gets interesting.

### Walkthrough

#### Finalizing an event
Once **every heat** in an event is scored:
1. Go to the event detail page.
2. Click **"Finalize Event."**
3. The app calculates positions, awards points (college) or payouts (pro), and locks the event.
4. The Show Day Dashboard immediately updates standings.

#### What "finalize" does
- **College:** Awards individual points (1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1) and rolls them into team totals.
- **Pro:** Distributes the configured payouts (1st place gets the 1st payout, etc.). Updates the Pro Payout Summary.
- **Locks** the event so no more edits.

#### Ties
- **Time-based events:** Genuine ties (same time to the hundredth) are rare but possible. The app flags the tied competitors and marks the event as "Throwoff Pending."
- **Hard Hit events:** Ties are common. The app uses the configured tiebreak (number of hits in tied position, or a throwoff round).
- **When you see "Throwoff Pending":** Run a throwoff between the tied competitors physically, then enter the result via the **Throwoff Result** button on the event page. The app re-calculates positions and removes the pending flag.

### Practice drill
1. Score every heat of one event in the demo tournament.
2. Click Finalize Event.
3. View the event results — confirm positions and payouts/points look right.
4. Find a finalized event and try to edit it. Confirm it's locked.

### Gotchas
- **Don't finalize early.** If you finalize an event before all heats are entered, the standings will be wrong. Double-check the heat list shows every heat as Completed before clicking Finalize.
- **Once finalized, results count.** Standings displayed on the spectator portal and the announcer's screen will reflect the new placements within seconds. Be sure before you click.
- **Throwoffs do NOT need a new heat.** Just enter the throwoff result via the special button.

---

## Module 6 — The Show Day Dashboard (Your Cockpit on Saturday)

### Purpose
On Saturday, the pro show runs in **flights** — blocks of 8 heats from different events shown back-to-back. The Show Day Dashboard is your single screen for managing the pro show in real time.

### Walkthrough

1. From the sidebar, click **Show Day Dashboard.**
2. You'll see **flight cards** showing:
   - **Live flight** (currently running) — highlighted at the top
   - **Upcoming flights** in order
   - **Completed flights** (collapsed)
3. Inside each flight card:
   - The list of heats in flight order (1st heat, 2nd heat, ...)
   - Each heat shows event name, competitors, status (pending/in progress/completed)
   - A **"Score this heat"** button on the current heat
4. As each heat finishes physically, click "Score this heat", enter results, save. The dashboard updates immediately.
5. When all heats in a flight are completed, mark the flight as **"Complete."** The next flight automatically becomes the live flight.
6. **The dashboard auto-refreshes every 60 seconds.** You don't have to keep hitting refresh.

### Practice drill
1. Open the Show Day Dashboard.
2. Find the current "live" flight (or set the first flight live).
3. Enter scores for the first heat in the live flight.
4. Find the next heat without going back to a different page.

### Gotchas
- **Run heats in flight order.** The flight builder has already arranged them so events are mixed and competitors get rest between their own events. Skipping around defeats the purpose.
- **If a heat has to be skipped or moved**, talk to the head judge first. They'll edit the flight from the Build Flights page.
- **Saturday college overflow events** (Chokerman's Race Run 2, occasional standing block events) appear in the flight list near the end of the last flight. Score them like any other heat.

---

## Module 7 — Reading Standings, Payouts, and Reports

### Purpose
After events are finalized, you'll be asked questions: "What's my placement?" "How many points did UM-A get?" "What's my payout?" Know where to find the answers.

### Walkthrough

#### College standings
1. Sidebar → **College Standings** (or Show Day Dashboard → Standings panel).
2. You'll see:
   - **Bull of the Woods** — top male individual
   - **Belle of the Woods** — top female individual
   - **Team Standings** — all teams ranked by total points
3. Click any team to drill into individual member contributions.
4. Click **Print** for a clean printable version.

#### Pro payout summary
1. Sidebar → **Payout Summary** under Results.
2. You'll see every pro competitor with their total earnings, broken down by event.
3. Use this for check writing at the end of the day.
4. **Mark each competitor "Settled"** as they collect their check via the Payout Settlement page. This produces a paper trail.

#### Event results
1. Sidebar → **All Results.**
2. Click any event to see the full placement list.
3. Click **Print** for a printable version that you can hand to the announcer.

#### Exporting to Excel
1. Tournament detail page → **Export Results.**
2. An Excel file downloads with every competitor, every event, every result.
3. Use this to hand off to the official scorer or as a year-end archive.

### Practice drill
1. Pull up College Standings and identify Bull of the Woods.
2. Print the Pro Payout Summary.
3. Export results to Excel.
4. Find one competitor's individual results page (use the Competitor Detail link from the registration list).

### Gotchas
- **Standings only update when events are finalized.** If a finalized event isn't reflected in standings, refresh the page.
- **The spectator portal shows the same standings** at a different URL — competitors and the public can watch live. So if a standing is wrong, fix it fast.

---

## Module 8 — Special Events: Pro-Am Relay, Partnered Axe, Birling

### Purpose
Three events have unusual workflows. Learn each one once.

### Walkthrough

#### Pro-Am Relay
1. Sidebar → **Pro-Am Relay.**
2. Before the show: click **"Run Lottery."** The app randomly assigns 2 pro men + 2 pro women + 2 college men + 2 college women to each of two teams.
3. Print the team rosters. Hand to team captains.
4. The relay runs as one flight with four sub-events (partnered sawing, standing block, underhand block, team axe throw).
5. Enter each sub-event's result on the relay results page.
6. The app calculates which team won the relay — relay results do NOT count toward college team or individual standings (this is the only event where college competitors win cash).

#### Partnered Axe Throw
1. Sidebar → **Partnered Axe Throw.**
2. **Prelims** run before the show: every pair throws, you enter their hits.
3. The app identifies the **top 4 pairs** automatically.
4. **Finals** run during the show, one pair per flight.
5. Enter finals scores as they finish.
6. Final standings combine: finals for positions 1–4, prelim standings for positions 5+.

#### Birling (College only)
1. Sidebar → **Events** → click on Men's Birling or Women's Birling.
2. The bracket displays pre-seeded matchups.
3. Click any matchup to enter the **winner**.
4. Winners advance up the bracket. Losers drop into the losers bracket (double elimination).
5. The app handles all the bracket math. You just click the winners.

### Practice drill
1. Run a fake relay lottery on the demo tournament. Confirm the gender balance is correct.
2. Enter prelim scores for partnered axe throw. Confirm the top 4 are identified correctly.
3. Click through a few birling bracket matches and confirm the bracket advances correctly.

### Gotchas
- **The relay lottery can be re-run** if needed. The previous draw is replaced.
- **Partnered Axe Throw "Finals" cannot start until prelims are finalized.** The app enforces this.
- **Birling brackets are pre-seeded** — don't manually re-seed unless told to.

---

## Module 9 — Pre-Show Setup: Registration and Event Configuration

### Purpose
Before the show starts, the app needs to know who is competing and what events are running. This work is done in the days and weeks before the tournament, but as a judge you'll often be the one doing it (or fixing it when it's wrong).

### Walkthrough

#### Creating the tournament
1. Dashboard → **New Tournament.**
2. Enter the name (default "Missoula Pro Am") and the year.
3. Set the **college date** (Friday) and **pro date** (Saturday). If there's a Friday Night Feature, set its date too.
4. Click **Create Tournament.** You're taken to the new (empty) Tournament Detail page.

#### Cloning last year's tournament
This saves a lot of typing if the event list is mostly the same:
1. From an existing tournament → **Clone Tournament.**
2. Confirm.
3. The new tournament copies all event configurations, payout amounts, wood specs — but NOT competitors or results.
4. Update the dates and year, then continue.

#### Importing college teams
1. From Tournament Detail → Show Entries → **Import Teams.**
2. Click "Choose File" and select a college entry form (Excel `.xlsx` or `.xls`).
3. Click **Upload.**
4. The app reads the file, creates the team and its competitors, and reports any errors.
5. If a team has problems (too few women, missing info), it's flagged in red on the team list. Click the team to see the specific errors and fix them via the form.
6. Repeat for each team's entry form.

#### Registering pro competitors
Two ways:
- **Manual entry**, one at a time: Show Entries → Pro Competitors → **Add New Competitor.** Fill in name, gender, contact, shirt size, ALA status, lottery opt-in, left-handed flag.
- **Bulk Excel import**: Show Entries → **Import Pro Entries.** Upload the entry form Excel. The app parses, shows you a review screen with duplicate detection and alias matching, and you confirm.

#### Configuring events
1. Tournament Setup page → **Events tab.**
2. Check which events are running this year.
3. For traditionally OPEN college events (Axe Throw, Peavey, Caber, Pulp Toss), choose whether to run them as OPEN (anyone competes) or CLOSED (capped at 6 entries per athlete).
4. For each eligible event (underhand, standing block, springboard SPEED only — never Hard Hit), choose **Championship** (no handicap) or **Handicap** (start marks calculated by STRATHMARK).
5. Click **Save Configuration.**

#### Setting payouts
1. From the events list, click an event.
2. Click **Configure Payouts.**
3. Enter dollar amounts for 1st, 2nd, 3rd, etc. Up to 10 places.
4. Or apply a **Payout Template** — reusable saved structures (e.g. "Standard Pro Sawing", "Hard Hit Premium").
5. Save.

### Practice drill
1. Clone last year's demo tournament.
2. Update the dates to the current year.
3. Import a college entry form.
4. Manually add one pro competitor.
5. Configure payouts for one event using a template.

### Gotchas
- **OPEN vs CLOSED matters.** OPEN events let anyone enter regardless of count; CLOSED enforces a 6-event cap. The Missoula Pro Am sometimes runs traditionally OPEN events as CLOSED to save time. Confirm with the head judge.
- **Hard Hit events cannot be Handicap.** The toggle is hidden for them — this is intentional, not a bug.
- **Importing teams does NOT delete existing teams.** It adds. If you upload the same file twice, you get duplicates. The app warns you, but read the warning.
- **Cloning a tournament copies wood specs and event config — but not the dates.** Always update the dates after cloning, or you'll have a "2026" tournament with last year's dates.

---

## Module 10 — Heat Generation and Flight Building

### Purpose
Once registration is closed, the app builds heats (for college events) and flights (for the pro show) automatically. You will trigger this and review the result. Understanding what the algorithm is doing helps you fix it when something looks wrong.

### Walkthrough

#### Generating college heats
1. From the Events page, click an event.
2. Click **Generate Heats.**
3. The app creates heats based on:
   - Number of competitors signed up
   - Stand capacity (e.g., 5 underhand stands = 5 per heat)
   - Partner assignments (partners stay in the same heat)
   - Gender (segregated for most events)
   - Two-run logic (creates Run 1 and Run 2 heats automatically; stand assignments swap between runs)
4. Review the resulting heats on the heat list page.

There's also a one-click bulk action: **"Generate All College Heats"** on the Events & Schedule page. Use this once you've configured everything and you're confident in the entries.

#### Generating pro heats
Same flow — go to each pro event and click Generate Heats. Special rules:
- **Springboard:** Left-handed cutters are grouped together on the same dummy. The "slow heat" flag puts known slow cutters together.
- **Gear sharing:** Competitors who share equipment are placed in different heats automatically.
- **Ability ranking** (optional): if you've assigned ability ranks via the Ability Rankings page, the heat generator groups competitors by skill level instead of randomly.

#### Setting ability ranks (optional, recommended for springboard)
1. Sidebar → **Ability Rankings** under Show Configuration.
2. Pick an event category (e.g., springboard).
3. Drag-rank or type a number 1–N for each competitor.
4. Save. Re-generate the affected heats to apply.

#### Building flights for the pro show
1. From the Pro Dashboard or Events & Schedule page, click **Build Flights.**
2. Set the heats-per-flight (default 8).
3. Click **Build Flights.**
4. The app uses a greedy algorithm to:
   - Mix events within each flight (variety for the crowd)
   - Ensure each competitor has at least 4–5 heats between their own appearances (rest time)
   - Open each flight with a springboard heat (when one is available)
   - Avoid scheduling Cookie Stack and Standing Block in the same flight (they share stands)
5. Review the flight list. Each flight is a card showing the heats in order.

#### Reordering flights or heats
- Drag-and-drop in the flight list to swap flight order.
- Drag-and-drop within a flight to change heat order.
- Save changes.

#### Heat sync
Heats are stored in two places internally (a JSON list and a separate assignment table). If they ever drift, you'll see a **"Sync Issue"** badge. Click the **Sync** button to fix it. This is rare but important.

### Practice drill
1. Generate heats for one college chopping event.
2. Generate heats for the entire college day in one click.
3. Set ability ranks for 5 pro springboard competitors.
4. Generate springboard heats and confirm the rankings shaped the grouping.
5. Build flights for the pro day.
6. Reorder one flight by dragging.

### Gotchas
- **Re-generating heats DELETES existing heats and any scores entered against them.** The app warns you with a confirmation dialog. Never re-generate after the show has started without head judge approval.
- **Flight building should happen AFTER all pro heats are generated.** Otherwise the flights will be missing events.
- **Spillover events** (Saturday college overflow like Chokerman's Race Run 2) are added to the END of the last flight automatically. You don't have to do anything special — but you do have to flag the events as "Saturday overflow" first via the Saturday Priority page.
- **Cookie Stack and Standing Block share 5 physical stands.** The flight builder enforces that they can't both run in the same flight. If you see a conflict warning, it means you've tried to schedule them together — separate them.

---

## Module 11 — Preflight Checks and Validation

### Purpose
Before the show starts, the app can run a battery of automatic checks to catch problems while there's still time to fix them. Use this religiously.

### Walkthrough

#### Running the preflight check
1. Sidebar → **Preflight Check** under Show Configuration.
2. The page runs all checks automatically and shows a status report:
   - **Heat/table sync** — Do all heats have matching assignment rows?
   - **Odd partner pools** — Any partnered events with an odd number of pairs?
   - **Saturday overflow status** — Are spillover events flagged correctly?
   - **Gear sharing conflicts** — Any heats with two gear-sharing partners in the same heat?
   - **Missing data** — Competitors without genders, events without scoring types, etc.
3. Each check shows green (pass), yellow (warning), or red (failure).
4. Click any failed check to see the affected records and a "Fix" link.

#### Validation Dashboard
1. Sidebar → **Validation** under Show Configuration.
2. Three tabs: **Teams**, **College Competitors**, **Pro Competitors**.
3. Each tab shows records that fail validation rules (e.g., a team with fewer than 2 women, a competitor with no events).
4. Fix the issue from the linked detail page, then re-run validation.

### Practice drill
1. Run a preflight check on the demo tournament.
2. Read every yellow and red item.
3. Fix at least one issue and re-run to confirm it clears.

### Gotchas
- **Always run preflight the morning of the show.** Late entries and last-minute scratches frequently break validation in subtle ways.
- **A yellow warning is not always a problem** — for example, an odd partner pool might be intentional if one person is pairing with a registered solo. Use judgment.
- **Don't ignore red errors.** They block heat generation or score entry for the affected event.

---

## Module 12 — The Gear Sharing Manager

### Purpose
Pro competitors share expensive equipment (springboards, hot saws, single bucks). Two people sharing one saw cannot be in the same heat — they need to physically swap. Managing this is one of the trickiest parts of running the pro show, and the app has a dedicated tool for it.

### Walkthrough

#### Opening the Gear Sharing Manager
1. Sidebar → **Gear Sharing** under Show Entries.
2. The page shows several panels:
   - **Verified Pairs** — confirmed sharing relationships, both directions logged
   - **Unresolved Entries** — one competitor said they share with X, but X hasn't confirmed
   - **Heat Conflicts** — heats where two gear-sharing partners are scheduled together
   - **Parse Review** — free-text gear-sharing notes from entry forms that need human interpretation
   - **College Constraints** — gear sharing on the college side
   - **Stats** — counts and summary

#### Reviewing parsed entries
1. Click **Parse Review** if there are entries waiting.
2. Each row shows the original free-text note, the parsed interpretation, and a confirm/reject button.
3. Confirm what's right; correct what's wrong.

#### Creating a gear group
For three or more people sharing one item (rare but happens):
1. Click **Create Gear Group.**
2. Pick the event (e.g. springboard).
3. Select all competitors in the group.
4. Name the group (e.g. "Smith Family Springboard").
5. Save. The heat generator now treats them as mutually exclusive in the same heat.

#### Auto-fixing heat conflicts
1. If the Heat Conflicts panel shows entries, click **Auto-Fix Conflicts.**
2. The app makes multi-pass swap attempts within the same run to separate gear-sharing partners.
3. Read the result: how many were fixed, how many failed.
4. Failed conflicts need manual heat editing — open the heat and move someone.

#### Cleaning up scratched competitors
- Click **Clean Scratched.** Removes any gear-sharing references to scratched competitors.

### Practice drill
1. Open the Gear Sharing Manager.
2. Run the parse review.
3. Manually create a gear group for two springboard cutters.
4. Re-generate that event's heats and confirm the two are not in the same heat.

### Gotchas
- **Gear sharing is bidirectional.** Joe shares with Jim AND Jim shares with Joe. If only one direction is recorded, it's "unresolved" and the heat generator may not catch the conflict.
- **Free-text entries are messy.** "shares saw with Kaper" is parseable; "borrowing equipment" is not. Always run Parse Review before generating heats.
- **Auto-fix is not magic.** If three people in the same heat all share gear with each other, no swap can fix it — the heat needs manual surgery.

---

## Module 13 — Wood Specs and the Virtual Woodboss

### Purpose
Every chopping and sawing event needs wood prepared in specific species and sizes. The Virtual Woodboss tells the wood crew exactly what to cut, how many of each, and shows projected linear feet of saw logs. This was traditionally a clipboard-and-spreadsheet job; now the app does it.

### Walkthrough

#### Configuring wood specs
1. Tournament Setup page → **Wood Specs tab.**
2. For each event category (underhand, standing block, springboard, single buck, double buck, etc.) and each gender, set:
   - **Species** (e.g., aspen, white pine, cottonwood)
   - **Size** (e.g., 12 inches diameter, or 300 mm)
   - **Notes** (optional — e.g., "use the dry stack")
3. Set **general saw log** specs (used by stock saw, single buck, double buck, jack & jill).
4. Set **relay block** specs separately — Pro-Am Relay uses its own wood.
5. Save.

#### Generating the woodboss report
1. Sidebar → **Virtual Woodboss.**
2. Click **Generate Report.**
3. The page shows:
   - Block counts per event (driven by competitor count × heats)
   - Saw log linear feet
   - Pro-Am relay blocks
   - Total wood needed by species
4. Click **Print Report.** Hand to the wood crew.
5. There's also a **Lottery View** (manual count overrides for events where the count is determined by lottery, like relay) and a **History** view (cross-tournament wood usage trends).

#### Sharing with the wood crew (no login required)
1. From the woodboss page, click **Generate Share Link.**
2. The app creates a signed URL that the wood crew can open on their phones without logging in.
3. Send it to them. The link is read-only.

### Practice drill
1. Open Tournament Setup → Wood Specs.
2. Set species and sizes for at least three event categories.
3. Generate a woodboss report.
4. Print it.
5. Generate and copy a share link.

### Gotchas
- **Counts are driven by registered competitors.** If you generate the report before everyone is registered, the counts will be low. Re-generate after final registration.
- **Stock saw falls back to the general log specs** if you don't set stock saw specifically. This is intentional — usually they're the same.
- **Pro-Am relay block counts come from manual override**, because the relay teams are determined by lottery on show day. Enter the team count after the lottery runs.
- **Inches vs millimeters** — the app supports both. Pick one unit per spec; mixing them will confuse the wood crew.

---

## Module 14 — End of Show: Settlements, Backups, and Wrap-Up

### Purpose
After the last heat finishes, there's still 30–60 minutes of work: distribute payouts, settle fees, generate final reports, back up the database, and archive the tournament.

### Walkthrough

#### Pro payout settlement
1. Sidebar → **Payout Settlement** under Results.
2. The page shows every pro competitor with:
   - Total earnings
   - Per-event breakdown
   - Settled / Unsettled status
3. As each competitor collects their check, click **Mark Settled.**
4. The page tracks who's been paid and who hasn't.
5. Print the settlement page as your audit trail.

#### Pro entry fees
1. Sidebar → **Fee Tracker** under Results.
2. Per-competitor checklist of entry fees owed and paid.
3. Click expand to see per-event fee breakdown.
4. Toggle **"Outstanding only"** to see who still owes.
5. Mark fees as paid as cash comes in.

#### Final reports to print
- **All Results** — every event, every placement
- **College Standings** — Bull/Belle of the Woods, team standings
- **Pro Payout Summary** — every competitor's total earnings
- **Heat Sheets** — for archives

Print or export each. Save PDFs to a USB drive for the official record.

#### Excel export
1. Tournament Detail → **Export Results.**
2. Wait for the export job to complete (it runs in the background — you'll see a progress indicator).
3. Download the resulting Excel file.
4. Save it somewhere safe.

#### Database backup
1. Tournament Detail → **Backup Database** (or whatever the admin button is called).
2. The app writes a backup file to the configured location (cloud S3 or local `instance/backups/`).
3. **Always** make a manual backup at the end of the day, even if automatic backups are configured. Belt and suspenders.
4. If running locally: copy the `proam.db` file to a USB drive or cloud folder. That single file is your entire tournament.

#### Closing the tournament
1. Tournament Detail → set status to **Completed.**
2. The tournament now appears in the archive list and is locked from editing (admins can still un-lock for corrections).

### Practice drill
1. Mark three pro competitors as payout-settled.
2. Mark one fee as paid.
3. Run an Excel export and download the file.
4. Make a database backup.
5. Set a demo tournament status to Completed.

### Gotchas
- **Don't close out fees and payouts at the same desk.** They are different ledgers — entry fees are money coming IN, payouts are money going OUT. Settle them separately to avoid confusion.
- **Always export to Excel before closing the tournament.** The Excel file is your portable, readable, future-proof record. The database file is great until the year you can't find a working Python install.
- **A backup is not done until you have verified the file exists.** Open the backups folder, see the file, note the timestamp.
- **The Pro Payout Summary should match the cash you handed out, to the dollar.** If it doesn't, find the discrepancy before you go home.

---

## Final Exam: Dry Run a Half-Day

Before you ever sit at the scorer's tent on a real show day, do this dry run on the demo tournament:

1. Log in. Get to the Show Day Dashboard.
2. **Score every heat of three events** end to end — at least one chopping, one sawing, and one two-run event.
3. **Make a deliberate mistake** on one heat and fix it.
4. **Scratch a competitor** mid-day and verify it propagates.
5. **Finalize all three events.**
6. **Pull up standings** and confirm the math is right.
7. **Print** at least one heat sheet, one event result, and the payout summary.
8. **Export to Excel.** Open it. Confirm your three events appear.

If you can do all of this without referring back to this document, you're ready to judge a real show.

---

## Cheat Sheet (Print This — Keep at the Scorer's Tent)

### The 6 buttons you'll click 95% of the time
1. **Show Day Dashboard** — see what's happening now
2. **Score this heat** — enter results
3. **Save Results** — commit them
4. **Finalize Event** — lock it when done
5. **Heat Sheets** — print backup copy
6. **All Results** — answer "what was my time?"

### Time format
- Always seconds with decimals: `74.83` not `1:14.83`
- Tab key moves to the next field

### Status options
- **Completed** — they finished, score recorded
- **DNF** — started, didn't finish (placed last)
- **Scratched** — pulled before the heat (no placement)

### When in doubt
- Refresh the page (F5)
- Check the audit log (Admin → Audit Log) for the history of any change
- Ask the head judge

### Things that ARE recoverable
- Wrong score (just edit the heat)
- Wrong competitor in a heat (Edit Heat → move them)
- Accidental scratch (set status back to active)

### Things that are HARDER to recover
- Finalizing an event with wrong scores (admin has to un-finalize)
- Closing the browser without saving (lost typed scores)
- Running the relay lottery during the show (re-run it, but talk to head judge first)

### People you can call
- **Head judge** — for any rule, fault, or scoring decision
- **System administrator** — for any app bug, login problem, or anything broken on screen
- **Athlete** — for any question about *which* events they're entered in (it's on their heat sheet)

---

## Appendix A — Common Phrases the App Uses

| Phrase | What it means |
|---|---|
| "Throwoff pending" | Two or more competitors tied; physically run a throwoff and enter the result. |
| "Locked by [name]" | Another scorer is editing this heat; don't double-enter. |
| "Stale data — reload" | Someone updated this since you opened it; reload before saving. |
| "Heat conflict" | Two competitors sharing gear are scheduled in the same heat; tell the head judge. |
| "Unscored heats: 5" | 5 heats are pending and need score entry. |
| "Event finalized" | Locked. Edits require an admin to un-finalize. |
| "Outlier flag" | A score is wildly different from the heat average; double-check before saving. |

## Appendix B — A 60-Second Tour for Day-Of Refresher

If you only have a minute before the show starts:

1. Log in.
2. Click **Show Day Dashboard**.
3. The current flight is at the top. Heats run **top to bottom**.
4. Click a heat → **Score this heat** → type times → **Save**.
5. When the event's last heat is scored → **Finalize Event**.
6. Repeat until the day is done.

That's the entire job. Everything else in this document is for when something goes sideways.

---

*Curriculum version 1.0 — 2026-04-06*
*Companion to: USER_GUIDE.md, README.md, CLAUDE.md*
