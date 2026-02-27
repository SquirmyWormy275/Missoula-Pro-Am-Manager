# Missoula Pro Am Manager

A web-based tournament management system for the Missoula Pro Am timbersports competition.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Then open your browser to `http://localhost:5000`

---

## User Guide

Welcome! This guide will help you use the Missoula Pro Am Tournament Manager software to run your competition smoothly.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Creating a Tournament](#creating-a-tournament)
3. [Friday: College Competition](#friday-college-competition)
4. [Saturday: Pro Competition](#saturday-pro-competition)
5. [Entering Scores](#entering-scores)
6. [Viewing Results & Reports](#viewing-results--reports)
7. [Special Events](#special-events)
8. [Troubleshooting](#troubleshooting)

---

## Getting Started

### Starting the Program

1. Open a Command Prompt or Terminal window
2. Navigate to the program folder
3. Type: `python app.py` and press Enter
4. Open your web browser (Chrome, Firefox, or Edge recommended)
5. Go to: `http://localhost:5000`

You should see the main dashboard.

### Main Dashboard

The dashboard shows:
- **Active Tournament** - If you have one in progress, you'll see a quick link to it
- **All Tournaments** - List of past and current tournaments
- **New Tournament** button - Create a new tournament

---

## Creating a Tournament

1. Click **"New Tournament"** on the dashboard
2. Enter the tournament name (default: "Missoula Pro Am")
3. Enter the year (e.g., 2026)
4. Click **"Create Tournament"**

You'll be taken to the tournament detail page where you can:
- Set up college teams
- Register pro competitors
- Configure events

---

## Friday: College Competition

### Step 1: Import Teams

College teams submit Excel entry forms. To import them:

1. From the tournament page, click **"Import Teams"** under College Competition
2. Click **"Choose File"** and select the Excel file (.xlsx or .xls)
3. Click **"Upload"**
4. The system will import the team and all competitors

Repeat for each team's entry form.

**What gets imported:**
- Team name and school
- All competitor names and genders
- Events each person is entered in
- Partner assignments for partnered events

### Step 2: Configure Events

1. Click **"Configure Events"** from Quick Actions
2. Choose which events to run
3. For OPEN events, decide if they should be treated as CLOSED
4. Click **"Save Configuration"**

**OPEN vs CLOSED Events:**
- **OPEN Events**: Anyone can compete, even against other teams
- **CLOSED Events**: Each athlete can only enter up to 6 closed events

### Step 3: Generate Heats

For each event:

1. Go to **"View All Events"**
2. Click on an event name
3. Click **"Generate Heats"**

The system will automatically create heats based on:
- Number of competitors
- Number of stands/stations available
- Partner assignments (partners go in the same heat)

### Step 4: Run the Competition

1. Go to the **College Dashboard**
2. Select an event
3. View the heat list to know who competes when
4. After each heat finishes, enter the results (see [Entering Scores](#entering-scores))

### Step 5: View Standings

The College Dashboard shows:
- **Bull of the Woods** - Top male competitors by points
- **Belle of the Woods** - Top female competitors by points
- **Team Standings** - Teams ranked by total points

---

## Saturday: Pro Competition

### Step 1: Register Competitors

1. From the tournament page, click **"Register Competitors"** under Pro Competition
2. Click **"Add New Competitor"**
3. Fill in:
   - Name, gender, contact info
   - Shirt size
   - ALA membership status
   - Pro-Am Relay lottery opt-in
   - Left-handed (for springboard)
4. Click **"Save"**

Repeat for each pro competitor.

### Step 2: Enter Event Sign-Ups

For each competitor:

1. Click on their name in the competitor list
2. Check the events they want to enter
3. Enter their partner info for partnered events
4. Note any gear sharing (who they share equipment with)
5. Click **"Save"**

### Step 3: Configure Payouts

For each pro event:

1. Go to the event from the Events list
2. Click **"Configure Payouts"**
3. Enter the payout amount for each place (1st, 2nd, 3rd, etc.)
4. Click **"Save"**

### Step 4: Generate Heats

Same as college - go to each event and click **"Generate Heats"**

### Step 5: Build Flights

Flights group heats from different events together for the show format.

1. Go to **"Build Flights"** from the Pro Dashboard
2. Review the settings (default: 8 heats per flight)
3. Click **"Build Flights"**

**Important:** The system automatically:
- Mixes different events in each flight (keeps crowd engaged)
- Ensures each competitor has at least 4-5 heats between their events (gives athletes rest time)

### Step 6: Run the Show

1. Go to the **Pro Dashboard**
2. View flights in order
3. Run each heat within the flight
4. Enter results as heats complete

---

## Entering Scores

### For Regular Events

1. Click on the event
2. Click on the heat you want to score
3. Click **"Enter Results"**
4. For each competitor:
   - Enter their time, score, distance, or hits
   - Mark as "DNF" or "Scratched" if applicable
5. Click **"Save Results"**

### For Two-Run Events (Speed Climb, Chokerman's Race)

These events give each competitor two runs:

1. Enter Run 1 results for all competitors
2. Enter Run 2 results for all competitors
3. The system automatically uses their **best run** as their final result

### For Birling (Bracket Events)

1. Go to the Birling event
2. The bracket shows matchups
3. Click on a matchup to enter the winner
4. Winners advance through the bracket

### Finalizing Results

After all heats are complete:

1. Go to the event
2. Click **"Finalize Results"**
3. The system will:
   - Calculate final positions
   - Award points (college) or payouts (pro)
   - Update standings

---

## Viewing Results & Reports

### College Standings

1. Go to **College Dashboard**
2. Click **"View Full Standings"**
3. See Bull/Belle of the Woods and Team Standings
4. Click **"Print Version"** for a clean printable page

### Event Results

1. Go to **"View All Results"**
2. Click on any event to see detailed results
3. Click **"Print"** for a printable version

### Pro Payout Summary

1. Go to **Pro Dashboard**
2. Click **"Payout Summary"**
3. See each competitor's total earnings
4. Click **"Print"** for check-writing

### Exporting to Excel

From the tournament page:
1. Click **"Export Results"**
2. An Excel file will download with all results

---

## Special Events

### Pro-Am Relay

The Pro-Am Relay pairs professional athletes with college competitors:

1. Go to the **Pro-Am Relay** section
2. Click **"Run Lottery"**
3. The system randomly pairs:
   - Pro men with college women
   - Pro women with college men
4. View and print team assignments

### Partnered Axe Throw

This event has preliminary rounds, then finals:

1. **Prelims**: All teams throw, scores recorded
2. **Top 4 Advance**: System identifies top 4 teams
3. **Finals**: Top 4 throw again (one per flight)
4. Final rankings determined

---

## Troubleshooting

### The program won't start

**Problem:** You see an error when running `python app.py`

**Solutions:**
1. Make sure Python is installed: Open Command Prompt, type `python --version`
2. Make sure you're in the right folder: Use `cd` to navigate to the program folder
3. Install requirements: Run `pip install -r requirements.txt`

### Excel file won't import

**Problem:** Error when uploading college entry form

**Solutions:**
1. Make sure the file is .xlsx or .xls format
2. Make sure the file isn't open in Excel (close it first)
3. Check that the file has the expected columns (Name, Gender, School, etc.)
4. Try saving the Excel file as a new file and uploading the new one

### Scores aren't saving

**Problem:** You enter scores but they disappear

**Solutions:**
1. Make sure you click **"Save"** after entering scores
2. Check that you entered valid numbers (no letters or special characters)
3. Refresh the page and try again

### Standings don't look right

**Problem:** Points or standings seem incorrect

**Solutions:**
1. Make sure all heats for the event are marked complete
2. Click **"Finalize Results"** on the event
3. If a result was entered wrong, edit it and re-finalize

### Can't access the program

**Problem:** Browser shows "can't connect" or "page not found"

**Solutions:**
1. Make sure the program is running (you should see messages in the Command Prompt)
2. Make sure you're going to `http://localhost:5000` (not https)
3. Try a different browser
4. Restart the program (Ctrl+C in Command Prompt, then `python app.py` again)

### Competitor is in wrong heat

**Problem:** Someone was assigned to the wrong heat

**Solutions:**
1. Go to the event's heat list
2. Click on the heat
3. Click **"Edit Heat"**
4. Move the competitor to the correct heat
5. Save changes

### Need to scratch a competitor

**Problem:** Someone can't compete anymore

**Solutions:**
1. Find the competitor in the registration list
2. Click on their name
3. Click **"Scratch Competitor"**
4. They'll be marked as scratched and won't appear in future heats

### Data looks corrupted

**Problem:** Strange data or the program behaves oddly

**Solutions:**
1. Close the program (Ctrl+C)
2. Make a backup copy of `proam.db` (this is your database)
3. Restart the program
4. If problems persist, you may need to restore from a backup or start fresh

### Making a backup

Before each competition day, back up your data:

1. Close the program
2. Find the file called `proam.db` in the program folder
3. Copy it somewhere safe (USB drive, cloud storage, etc.)
4. Restart the program

---

## Quick Reference

### College Competition Workflow

1. Import team Excel files
2. Configure events (OPEN/CLOSED)
3. Generate heats for each event
4. Enter scores as heats complete
5. Finalize events to calculate positions
6. View/print standings

### Pro Competition Workflow

1. Register all competitors
2. Enter event sign-ups and partners
3. Configure payouts for each event
4. Generate heats
5. Build flights
6. Run show by flight order
7. Enter scores
8. View/print payout summary

### Keyboard Shortcuts

- **Tab**: Move to next field when entering scores
- **Enter**: Submit/save forms
- **Ctrl+P**: Print current page

---

## Getting Help

If you run into problems not covered here:

1. Check that the program is running (messages appear in Command Prompt)
2. Try refreshing your browser
3. Restart the program if needed
4. Contact the system administrator

---

## For Developers

See [DEVELOPMENT.md](DEVELOPMENT.md) for technical documentation including:
- Architecture and project structure
- Data models
- API routes
- Configuration reference
- Future development considerations

---

*Last updated: February 2026 — V1.2.0*
