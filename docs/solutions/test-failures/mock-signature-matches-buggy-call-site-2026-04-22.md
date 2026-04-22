---
module: testing
date: 2026-04-22
problem_type: test_failure
component: testing_framework
severity: high
root_cause: mock_signature_drift
resolution_type: test_fix
symptoms:
  - "Unit tests pass. QA passes. Production code would crash at runtime."
  - "Codex review flags a real signature mismatch between production code and the function it calls."
  - "The sync-test mock of the real function has the SAME wrong signature as the production caller, so both sides agree on the bug."
tags:
  - "pytest"
  - "mocking"
  - "integration-boundary"
  - "codex-review"
---

# Tests pass when the mock has the same wrong signature as the buggy code it covers

## Problem

In V2.13.0 the Print Hub feature added `services/email_delivery.py::queue_document_email`, which submits an async SMTP send to `services.background_jobs.submit()`. The real signature is:

```python
def submit(label: str, fn, *args, metadata: dict | None = None, **kwargs) -> str:
```

My production call was wrong — I passed the function first:

```python
# WRONG
background_jobs.submit(_worker_send, log_id, recipients, subject, body, ...)
```

This passes `_worker_send` as the `label` string (never used except as a display name) and `log_id` (an int) as `fn`. The worker thread would then try to call the int as a function and crash — every email would stay stuck at `status='queued'` forever.

**65 unit tests passed.** The QA script passed. Codex review caught it in 2 minutes of diff reading.

## Symptoms

- Tests: all green.
- QA script (HTTP end-to-end via Flask test client): all green.
- Real production behavior: every email send would silently fail with a `TypeError: 'int' object is not callable` inside the background thread, invisible to the request handler.
- Evidence: `PrintEmailLog.status` stays `queued` forever, never transitions to `sent` or `failed`. AuditLog never sees `email_sent` or `email_failed`.

## What Didn't Work

- **Running the test suite.** All 3199 tests passed. The mock sync_submit had the same wrong signature as the production code:

  ```python
  # Test mock — matched the production bug
  def sync_submit(fn, *args, **kwargs):
      fn(*args, **kwargs)
  monkeypatch.setattr(bj, 'submit', sync_submit)
  ```

  So production called `sync_submit(_worker_send, log_id, ...)` and the mock happily executed `_worker_send(log_id, ...)`. Both sides agreed on the bug. The test assertions about `PrintEmailLog.status == 'sent'` passed because the mock short-circuited the broken path entirely.

- **Eyeballing the diff.** The production call pattern reads naturally — Python accepts arbitrary positional args — so nothing looks wrong to a reviewer who doesn't cross-reference the callee's signature.

## Solution

Fix the production code:

```python
# CORRECT — label first
background_jobs.submit(
    f"email:{doc_key}",   # label
    _worker_send,         # fn
    log_id,               # *args from here on
    recipients, subject, body, attachment_bytes,
    attachment_name, attachment_mime,
)
```

Update every mock to match the real signature:

```python
# Test mock — now matches the real signature
def sync_submit(label, fn, *args, **kwargs):
    fn(*args, **kwargs)
```

Commit: [services/email_delivery.py, tests/test_email_delivery.py, tests/test_print_hub_route.py, scripts/qa_print_hub.py] in commit `2e12127`.

## Why This Works

The mock and the caller share the same function signature, so a signature-level bug in the caller only gets caught when the mock's signature matches the REAL callee's signature. When you write the mock to match your (buggy) assumption about the callee, you've built a shared delusion between test and production.

## Prevention

### Rule: mocks must match the real function's signature, not the caller's expectation

When writing `monkeypatch.setattr(module, 'fn', fake_fn)`, copy the signature of the real `fn` — don't write the signature that "makes the test pass." If the test fails because the production call doesn't match, that failure IS the bug.

### Tactical checks

1. **Grep the real function's `def` line when writing a mock:**

   ```python
   # Before mocking, read the real signature:
   import inspect
   import services.background_jobs as bj
   print(inspect.signature(bj.submit))
   # (label: str, fn, *args, metadata: dict | None = None, **kwargs) -> str
   ```

   Paste that signature verbatim into your mock. If the production caller doesn't match, the test crashes — that's the bug signal.

2. **Prefer typed signatures over `*args, **kwargs` in mocks.** A loose mock (`def mock(*args, **kwargs)`) silently accepts any call pattern, which is exactly the mask this bug exploited.

3. **Use `spec=` on `unittest.mock.Mock` when the mock isn't just a function replacement:**

   ```python
   from unittest.mock import Mock
   import services.background_jobs as bj
   fake_submit = Mock(spec=bj.submit)
   monkeypatch.setattr(bj, 'submit', fake_submit)
   ```

   `spec=` enforces the real function's signature on the mock — calling with the wrong args raises `TypeError` at test time.

4. **For critical integration boundaries, have at least one test that calls the real function** (not a mock). The `background_jobs.submit` path in this codebase has no such test — the thread-pool handoff is only ever exercised via mocks. A single integration test that calls the real `submit()` with a no-op worker would have caught this immediately.

5. **Outside-voice review.** Codex (or any model that doesn't share the original author's assumptions) reads the diff cold and cross-references callee signatures. Two models agreeing is a strong signal; one model disagreeing on a cross-reference like this is often a real bug.

### Regression test

Add at least one test that asserts the CALL is made with the real signature, not just that side-effects work. For `queue_document_email`:

```python
def test_queue_document_email_passes_label_to_submit(monkeypatch, smtp_env):
    captured = {}
    def capture_submit(label, fn, *args, **kwargs):
        captured['label'] = label
        captured['fn'] = fn
    import services.background_jobs as bj
    monkeypatch.setattr(bj, 'submit', capture_submit)

    email_delivery.queue_document_email(
        tournament_id=1, doc_key='heat_sheets', entity_id=None,
        recipients=['a@b.com'], subject='s', body='b',
        attachment_bytes=b'', attachment_name='n.pdf',
    )
    assert isinstance(captured['label'], str)
    assert captured['fn'].__name__ == '_worker_send'
```

## Meta-lesson

**A unit test isn't evidence production works — it's evidence that the test and the code agree.** When both come from the same author in the same session, they can agree on a lie. An outside reviewer who reads the callee's signature independently is irreplaceable. Codex's one-shot read-only review caught three bugs my 3199-test suite missed — this one, a `User.email` AttributeError swallowed by an over-broad `except Exception`, and a security gate that the email POST wasn't enforcing but the GET route was.

When you're about to merge a large PR: get an outside-voice diff review. Not for style, not for opinions — for the cross-reference reading that a same-session author can't do on their own work.
