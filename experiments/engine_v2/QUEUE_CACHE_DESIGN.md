# Isolated queue diagnostics and cache-first classification

This is an OFFLINE experiment. It does not alter production scheduling, budgets,
identity guards, events, progress, operator decisions or publication approvals.
No news reader or live AI transport is provided. Import these helpers with the
repository root and `experiments/engine_v2` on `sys.path`, and use `engine.connect`
for an in-memory or disposable database outside the repository.

## Entry points

- `queue_diagnostics.legacy_cause`: explicit historical reason codes only.
  Ambiguous `budget_deferred`, `call_budget_deferred`, and unrecognized reasons
  remain `unknown`. Queue age/remaining money cannot prove an old failure cause.
- `queue_diagnostics.budget_causes`: read-only snapshot observations for daily
  and monthly money caps, daily and per-run communication caps, and processing
  slots. It returns every observed limit; it is NOT a reservation/permission to
  communicate. Existing `ai_cost_control.reserve` remains unchanged. This helper
  does not replace tracking reservations, billing holds, or the six-hour gate.
- `queue_diagnostics.failure`: SQLite-only, allowlisted stage, exception type,
  fixed safe error, SHA-256 request key, response completion and cause. Unknown
  free text is replaced wholesale, not guessed safe by a PII regex. Request and
  response bodies, URLs, names, headers, tokens, and arbitrary exception strings
  are never included in diagnostic rows. Unknown completion stays unknown.
- `queue_diagnostics.schedule_replay`: recorded/mock candidates and answers go
  through existing `lookup_event` validation on a copy. Selection, analysis, and
  validation failures are distinct. It never writes the candidate event. This
  makes diagnosis possible for newly recorded failures; it cannot recreate an
  old missing response or establish an old ValueError's cause.
- `status_cache_replay.run`: classification references and a shadow queue
  projection only, with finite `MockResponses` fixtures or no fresh responses.
  It preserves the order supplied by the caller. No priority policy changes.
  The caller supplies the complete current triage/registry context, the same
  context used for capture; an omitted context is an explicit empty fixture.

## Exact binding and invalidation

A reusable entry requires ALL of: complete request hash (model/input/output
limit), full original article payload hash (including URL, publication time,
description/body and additional fields), group and triage, operator context,
registry, stored events, and current validator/code revision. The cached payload
has a separate integrity checksum. Current source validity, existing inline
category/value validation and JSON parsing are run again; uncertain completion
and invalid text field types are rejected. Classification is not identity proof.

The prompt is extracted from `news_status.py` without executing that script.
The current category/value guard is reused from its AST. Changed source code
invalidates existing bindings, even if only a comment changed. Event context is
conservatively broad: this experiment does NOT narrow identity conditions.
Known observation timestamps and cached-skip counters alone do not invalidate
receipts. Manual status, decision attempts, operator inputs and unknown future
triage fields remain part of the validation context.

Legacy raw Responses caches have no reliable article/validation manifest. A
matching request key alone is insufficient, especially when the prompt does not
include the article URL or description. Such entries are recorded as unknown,
retained, and never automatically stamped with today's context. This version
only enables captures for explicit fictional/mock inputs. A saved-provenance
manifest importer and live-client integration are deliberately NOT enabled.

## Bounds, receipts and recovery

- Default/max cache validations: 16 per pass; max cache scan: 2,000 entries.
- Fresh/mock processing slots: default/max 4. Cache validations do not consume
  those slots. Live communication count is always zero.
- Bounded scanning has a persistent cursor. Candidates are never discarded.
- Attempt receipts bind input, semantic answer, provenance state and validation
  context. Response IDs/usage timestamps alone cannot reopen an unchanged
  answer. Repeating a rejection creates no new diagnostic or failure attempt.
  Changed articles, answers or conditions can be evaluated under new bindings.
- Success receipts and classification references share a SQLite transaction.
  Failed saves roll both back. Captured mock replies survive, so a retry need
  not consume another mock slot. Reopening the DB preserves idempotence.
- Sources, original holds, events and budget records are unchanged. Shadow
  `status_shadow_results` retains the classified group plus destination
  (`tracking` or `out_of_scope`); old source snapshots are not deleted. Only
  current bindings enter the returned queue counts. Downstream means pending
  identity/evidence review, NOT accepted event facts or publication permission.

`total_waiting` in the helper counts only its input groups plus derived tracking
references. It is not a whole-service article counter. For a service comparison,
count unique article revisions across this projection and the unchanged other
stages. Do not count a move to tracking as a completed judgment.

## Fictional comparison and tests

Two independent temporary DBs use the same six fictional one-article groups in
the same order, four identical fresh mock slots and two provenance-bound cache
fixtures beyond the initial four. One cached result needs tracking; one is out
of scope. Baseline: four classifications, two still awaiting classification,
four downstream, six total waiting. Cache-first: six classifications, zero
awaiting classification, five downstream, five total waiting. Both use four
mock requests and zero live communications. This illustrates queue mechanics,
not real-news accuracy, real billing savings or expected production throughput.

Run the existing isolation harness from the repository root:

```sh
python experiments/engine_v2/run_offline.py
```

New tests are `test_queue_diagnostics.py` and `test_status_cache_replay.py`.
They use fictional data only. Operational snapshots, private replay outputs,
raw caches and real exception payloads must not be committed with these files.

Citation binding, priority changes, a production writer, live network adapters,
automatic event merging, progress adoption and publication are out of scope.
