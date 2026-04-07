# Handicap Mark Assignment — Race-Day Workflow

**Applies to:** Missoula Pro-Am Manager V2.7.0+
**Owner:** Show Director / Head Judge

This document describes the two paths for assigning STRATHMARK handicap start
marks to a Handicap-format event, plus the offline pre-compute workflow that
keeps race day moving when Railway can't reach the local STRATHMARK Ollama
host.

---

## Path A — Live STRATHMARK call (default)

Use this when you're on the Pro-Am Manager UI **and** the STRATHMARK Supabase
backend is reachable (`STRATHMARK_SUPABASE_URL` + `STRATHMARK_SUPABASE_KEY`
env vars are set).

1. Open the event page: **Scheduling → [event] → Assign Marks**
2. Confirm the status cards show:
   - **Handicap Format:** Eligible
   - **STRATHMARK Connection:** Configured
3. Click **Assign Marks via STRATHMARK**.
4. The cascade runs server-side:
   `Manual > LLM (Ollama → Gemini cloud fallback) > ML > Baseline > Panel Fallback`.
   Whichever tier succeeds first wins.
5. Refresh the page; each competitor's start mark appears in the table.

If Ollama is unreachable from the host (race-day Railway is the common case),
the cascade automatically tries Gemini next (if `GEMINI_API_KEY` is set), then
falls through to ML / Baseline / Panel. **Total budget: under 5 seconds per
event** thanks to the new fail-fast (3s connect / 15s read) timeouts.

---

## Path B — Offline pre-compute + CSV upload (race-day safety net)

Use this when STRATHMARK is unreachable from the deployment **and** you have
no Gemini key set, **or** when you want to pre-compute marks at home with the
full local cascade and just push the result to Railway on the morning of the
show.

### Step 1 — Compute marks locally

On a laptop with the `strathmark` package installed and Ollama running:

```bash
# In the STRATHMARK repo
python -m strathmark.cli calculate \
    --event UH \
    --tournament "Missoula Pro-Am 2026" \
    --species cottonwood \
    --diameter-mm 305 \
    --output marks.csv
```

The CSV will look like:

```csv
competitor_name,proposed_mark
Alice Smith,4.5
Bob Jones,7.0
Carol White,12.0
```

> Either `competitor_name` or `competitor_id` is required. The mark column
> can be named `proposed_mark`, `mark`, `start_mark`, or `start_mark_seconds`.

### Step 2 — Upload to Pro-Am Manager

1. Open **Scheduling → [event] → Assign Marks** on the deployed app.
2. Scroll to **Upload Pre-Computed Marks (CSV)**.
3. Pick the CSV from Step 1 and click **Parse CSV & Preview**.
4. The preview table shows each row matched to a competitor in this event.
   - Rows with no warning are auto-matched.
   - Rows with a yellow warning need attention: unknown name, ambiguous
     name, missing ID, invalid mark, etc. **You can still type the mark
     directly into the form for a matched row even if the CSV warning
     applied to a different row.**
5. Override any individual mark by editing the input field.
6. Click **Confirm & Write Marks**.

The route writes `EventResult.handicap_factor` for every non-blank row,
clears `predicted_time` (because CSV-imported marks have no upstream
prediction to compare against), and audit-logs the action with
`source='csv_upload'`.

### Matching policy

| CSV row state                   | What happens                                          |
|---------------------------------|-------------------------------------------------------|
| Single name match               | Auto-matched, ready to confirm                        |
| Two competitors with same name  | Warning: ambiguous; unmatched until you disambiguate  |
| Name not in this event          | Warning: unknown; row stays unmatched                 |
| competitor_id supplied          | Direct lookup wins over name match                    |
| Negative mark (-2.5)            | Clamped to 0.0 with a warning                         |
| Non-numeric mark                | Warning, mark left blank                              |
| Blank mark                      | Warning, row preserved but skipped on confirm         |

The judge resolves all warnings manually before confirming. **No silent
guesses** — that's the entire point of the warning column.

---

## When to use which path

| Scenario                                                  | Path |
|-----------------------------------------------------------|------|
| Local development with Ollama running                     | A    |
| Railway with `GEMINI_API_KEY` set                         | A    |
| Railway with neither Ollama nor Gemini                    | B    |
| Pre-show practice with full cascade tuning                | B    |
| Last-minute mark override after seeing warm-up runs       | B (one row CSV is fine) |

---

## Race-day failure modes

| Symptom                                                  | Diagnosis                    | Fix                              |
|----------------------------------------------------------|------------------------------|----------------------------------|
| "STRATHMARK is not configured" warning                   | Env vars unset on Railway    | Use Path B (CSV upload)          |
| `assigned: 0, skipped: N` after Path A                   | Cascade fell to panel fallback for everyone | Check `GEMINI_API_KEY`; or use Path B |
| `no_wood_config` flash message                           | WoodConfig row missing for this event | Set wood specs in Tournament Setup → Wood Specs first |
| GET takes > 5 seconds                                    | Timeouts not honored; check STRATHMARK V0.4.0+ is installed | `pip install -U strathmark` |
| CSV preview shows all rows as "unknown"                  | competitor_name column doesn't match | Use `competitor_id` column instead |

---

## Related env vars (STRATHMARK V0.4.0+)

| Variable                          | Default                          | Purpose                                                       |
|-----------------------------------|----------------------------------|---------------------------------------------------------------|
| `OLLAMA_HOST`                     | `http://localhost:11434`         | Override Ollama host. Set to `disabled` to skip the tier.     |
| `STRATHMARK_OLLAMA_CONNECT_TIMEOUT` | `3`                            | TCP connect budget (seconds)                                  |
| `STRATHMARK_OLLAMA_READ_TIMEOUT`  | `15`                             | HTTP read budget (seconds)                                    |
| `STRATHMARK_OLLAMA_MAX_RETRIES`   | `0`                              | Race-day fail-fast — no retries by default                    |
| `GEMINI_API_KEY`                  | _(unset)_                        | Cloud fallback model. Unset = skip Gemini tier entirely.      |
| `GEMINI_MODEL`                    | `gemini-2.0-flash-lite`          | Cloud model name                                              |
| `STRATHMARK_SUPABASE_URL`         | _(unset)_                        | Required for Path A                                           |
| `STRATHMARK_SUPABASE_KEY`         | _(unset)_                        | Required for Path A                                           |

---

*Last updated: 2026-04-06 — V2.7.0*
