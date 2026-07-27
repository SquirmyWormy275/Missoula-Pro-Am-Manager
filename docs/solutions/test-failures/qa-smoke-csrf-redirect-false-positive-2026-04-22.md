---
module: testing
date: 2026-04-22
problem_type: test_failure
component: qa_tooling
severity: medium
root_cause: ad_hoc_requests_smoke_without_csrf_or_body_assertion
resolution_type: tooling_pattern
symptoms:
  - "Ad-hoc QA smoke via requests.Session() reports 200 on every auth-gated route."
  - "Operator believes the route works. In reality every 200 is the login page rendered by the CSRFError redirect chain."
  - "No status code or assertion catches the lie — follow_redirects=True masks the redirect through /auth/login."
tags:
  - "qa-tooling"
  - "csrf"
  - "flask-wtf"
  - "requests-session"
  - "app.test_client"
  - "sibling: hand-written-fixture-shape-divergence"
---

# QA smoke returns 200 on every gated route because the requests are getting the login page

## Problem (observed during V2.14.0 /ce-ship)

While running Phase 3 of `/ce-ship` for the V2.14.0 release, I needed a live-server smoke confirming the 5 new flight-fixes routes served 200 against the deployed version bump. Pattern used:

```python
import requests
s = requests.Session()
s.post('http://127.0.0.1:5055/auth/login',
       data={'username': 'STRATHEX', 'password': 'qa-smoke-temp'},
       allow_redirects=True)
for path in ('/scheduling/2/flights/build', '/scheduling/2/friday-night', ...):
    r = s.get(f'http://127.0.0.1:5055{path}')
    print(f'{r.status_code}  {path}')
```

All 5 routes printed **200**. I wrote in the /ce-ship report: *"all 5 flight-fixes routes serve 200 authenticated."* Shipped the release.

Codex-style self-review afterward revealed the smoke was a lie. The login had failed silently with a CSRF error, the server's `@app.errorhandler(CSRFError)` handler (V2.10.1) redirected to `/auth/login`, every subsequent gated request 302'd to `/auth/login?next=...`, and `requests.Session(..., allow_redirects=True)` faithfully followed every one of those redirects — reporting 200 because the login page is 200.

I had not smoked the feature routes. I had smoked the login page five times.

## Root cause chain

1. **Flask-WTF's `CSRFProtect`** is active on every POST form in the app. Pulled from `app.py`:
   ```python
   csrf = CSRFProtect()
   ...
   csrf.init_app(app)
   ```
   Without a matching token, every POST is rejected.

2. **V2.10.1 added a friendly CSRFError handler** (`@app.errorhandler(CSRFError)` in `app.py`) that 302-redirects HTML requests to the referrer (or `request.path`) with a flash — so the browser user sees "Your session expired" and can retry. For a POST to `/auth/login` without a token, the referrer is empty, so `request.path` = `/auth/login` and the redirect target is itself.

3. **`requests.Session()` does not automatically fetch the form** to extract `<input name="csrf_token" value="...">` before POSTing. It sends exactly the `data` dict passed in.

4. **`allow_redirects=True`** (the default) follows the 302 back to `/auth/login`, which renders the form with status 200. The session has a cookie now but no authenticated user.

5. **Every subsequent GET to a `MANAGEMENT_BLUEPRINTS` route** 302-redirects to `/auth/login?next=/scheduling/2/flights/build` (the Flask-Login unauthenticated guard). `allow_redirects=True` follows it. The final status is 200 — again, the login page.

6. **My smoke checked `r.status_code == 200`** without looking at the response body. Every route "passed."

## Verified repro

```
POST /auth/login (no csrf_token) → 302 Location: /auth/login
GET /scheduling/2/flights/build → 302 Location: /auth/login?next=/scheduling/2/flights/build
                                  → follows → 200 (login page)
```

With the right smoke (below):
```
GET /auth/login → 200, csrf_token captured
POST /auth/login (with token) → 302 Location: /judge
GET /scheduling/2/flights/build → 200
  title: "Build Flights - Missoula Pro Am (flights test)"
```

## Two correct patterns

### Pattern A: `app.test_client()` + direct session injection (preferred)

Use the Flask test client and inject the user_id straight into the session. No HTTP, no CSRF, no real network — and it's the pattern `scripts/qa_print_hub.py` already uses:

```python
from app import create_app
app = create_app()
client = app.test_client()
with client.session_transaction() as sess:
    sess['_user_id'] = str(admin_user.id)
# Now every client.get() is authenticated.
resp = client.get(f'/scheduling/{tid}/flights/build')
assert resp.status_code == 200
assert b'Build Flights' in resp.data  # body assertion, not just status
```

Downsides: no middleware / proxy / static-file behavior — it's a WSGI-level probe, not a real HTTP roundtrip. But that's usually fine for "does the route 200 and render the expected thing."

### Pattern B: live server + CSRF-aware requests session

If you need a real HTTP request (e.g. to smoke a deploy URL), fetch the form first:

```python
import re, requests

s = requests.Session()
r = s.get('http://host/auth/login')
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
r = s.post('http://host/auth/login',
           data={'username': user, 'password': pw, 'csrf_token': token},
           allow_redirects=False)
assert r.status_code == 302
assert r.headers['Location'] != '/auth/login'  # would mean login failed
# Now follow-up GETs with the session are authenticated.
```

## Always assert on the body, not just the status

The core lesson: **`assert resp.status_code == 200` proves a handler returned something — not that the handler you meant to hit returned it.** Pair every route smoke with a body assertion tied to a string that only appears in the intended response:

```python
assert resp.status_code == 200
assert b'Build Flights' in resp.data    # Phase 3 flights/build
assert b'PRO-AM RELAY' in resp.data     # Phase 4 relay tile
assert b'LH SPRINGBOARD' not in resp.data  # no contention warning in happy path
```

A login-page response is also 200. If the body check misses the login's `<title>Sign In</title>` it'll silently pass.

## Related footgun

The V2.14.0 codex P2 on [hand-written-fixture-shape-divergence-2026-04-22.md](hand-written-fixture-shape-divergence-2026-04-22.md) is the same shape of failure at a different layer:

- **Fixture-shape divergence:** test input data didn't match what the real service emits → passing test against unreal data.
- **QA smoke CSRF redirect:** test target didn't match what the real auth flow requires → passing test against the login page.

Both pass. Both lie. Both get caught by independent review reading *both sides* of the contract, not just one side.

## Prevention checklist for any new QA script

1. **Use `app.test_client()` + `session_transaction()`** for authenticated smokes unless you specifically need to exercise the real HTTP stack.
2. **When you do need real HTTP**, always GET the form first to extract the CSRF token before POSTing.
3. **Always assert on body strings** unique to the intended response, not just status codes.
4. **Run `allow_redirects=False` at least once** on the login POST. The Location header tells you exactly where the login chain ended up — if it's `/auth/login` instead of a post-login target, the smoke is compromised.

## Related docs

- [hand-written-fixture-shape-divergence-2026-04-22.md](hand-written-fixture-shape-divergence-2026-04-22.md) — sibling "test passed but lied" pattern at the JSON shape layer.
- [mock-signature-matches-buggy-call-site-2026-04-22.md](mock-signature-matches-buggy-call-site-2026-04-22.md) — sibling pattern at the function signature layer.
- `scripts/qa_print_hub.py` — reference implementation of Pattern A (test_client + session_transaction) across 36 checks.
- `app.py::_inject_csp_nonce` / `@app.errorhandler(CSRFError)` — V2.10.1 the CSRF redirect handler that makes the failure silent.
