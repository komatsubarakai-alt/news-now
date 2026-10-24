# Offline response provenance and quotation bindings

Production writers, networking, models, budgets, schedules and priority policies
are unchanged. All response/quote data belong in an isolated private SQLite DB,
not in public repository artifacts. Tests and the browser fixture are fictional.

## Capture before a response

`response_provenance.prepare_status(db, group, triage, registry, now)` freezes
the complete existing request hash, full input article hashes, URL/metadata
version IDs, metadata+body edition IDs, selected source states when available,
and current conditions/validator hashes BEFORE a response is supplied.
An unknown target has an empty scope; candidate event IDs are not verified
identity labels. `save` appends the provided response, local record ID, nullable
provider response ID, actual save time, completion, format result and conservative
reuse reason. It never invents an old provider ID, check time or article version.
`response_reuse_checks` retains the allowed/blocked result, reason, actual check
time and current conditions/version. Identical checks are deduplicated; a past
allowed record is never used instead of checking current conditions.

Then `install_status` may register it with the existing isolated cache-first
classifier ONLY after exact current input/condition matching and revalidation.
Even this is classification-only: no incident attachment, progress adoption,
operator truth label or publication approval. Invalid/incomplete responses are
retained with reuse blocked. `reusable` recomputes permission and a reason instead
of trusting an old approval flag. Tracking reuse additionally runs existing
identity/contradiction/save checks in an always-rolled-back savepoint.

Existing connections in the EXPERIMENT only:

- `status_cache_replay.capture` now records the provenance sidecar for newly
  supplied mock fixtures. Invalid input fixtures remain quarantined. It cannot
  upgrade a raw legacy response into a capture.
- `revision_sync.make_job` records original job input before `call_mock`.
  `call_mock` saves response and sidecar in the same transaction. The existing
  explicit adoption and publication validations remain required. Old cached
  jobs are not assigned retrospective response metadata.
- Future live integration would follow prepare -> existing budget-protected
  response call -> save -> current validation. No live adapter is implemented or
  enabled here. Do not pass old response files through that future arrival hook.

Raw legacy cache payloads/IDs already present in an imported snapshot are refused
by the new-response save hook. A request hash alone does not establish which
URL, article/body revision or validator generated the old response. Unknown
completion stays unknown. Metadata/hash tampering fails closed. Successful
operation replay returns the original record/time; conflicting replay is refused.
Save failures rollback; reopening the temporary DB preserves capture and receipts.

## Quotation records

`quote_bindings.record` preserves URL, explicit source state/version/edition,
article and quoted-component hashes, event scope, original quote and character
range, origin, optional response record, and save time. Character positions are
Python Unicode code-point offsets into the stored original field, not UTF-8 byte
offsets or normalized/HTML-stripped text. Ambiguous repeated text needs an explicit
range. No punctuation normalization, fuzzy replacement or invented body is used.

Kinds are headline, RSS description and body; `unknown` is allowed only for an
unbound old record. A matching headline is not body verification. An AI-origin
binding requires the original captured state/event scope AND a quotation actually
present in that captured response and its explicitly answered event ID. Merely
appearing in the candidate list does not authorize another event's quote. A
legacy quote without that provenance stays
unbound even if it happens to match the current headline. Explicit operator input
can create a NEW current-version record; it does not rewrite the old record.

Literal match, body confirmation and source confirmation are separate. Body and
source confirmations require an explicit actor/time and a current matching
operator reference linked to that evidence. They never create truth labels or
publication approvals. Cancellation or source changes make confirmations inactive
while retaining history. Event scopes on shared URLs are distinct, and cross-event
confirmation is rejected. Deleted/empty sources cannot provide new usable proofs.

Literal evidence is passed to the existing revision-sync evidence tables. Existing
operator review dependencies already watch metadata/body/availability. Article
changes therefore invalidate affected decisions; unrelated URLs remain untouched.
Old/unbound notes do not enter selectable proof IDs or weaken identity guards.

## Operator UI

Existing evidence cards show binding metadata and independent confirmation states.
Unbound quotes appear in a warning foldout and have no evidence checkbox.
The local-only `quote_record` action requires the current task/context, explicit
confirmation and event scope. Selecting a saved metadata version is an explicit
operator action; no body/source confirmation follows automatically. Body quotes
require a stored body. `quote_confirm` requires an existing matching judgment.
Both actions use the existing revision, origin/token and operation-receipt guards.
Neither action adopts facts or approves publication. Offline HTML disables saves.

No operator confirmation is performed automatically during real-snapshot tests.
Keep those reports/HTMLs private. A stored validation-time source snapshot may
allow a literal comparison; that is not proof of the version used at AI generation.

## Tests

```sh
python experiments/engine_v2/run_offline.py
python experiments/engine_v2/provenance_browser_check.py --output-dir /tmp/provenance-fictional-browser
```

The browser check uses only fictional in-memory fixtures, blocks all requests,
and checks 360/393/412px widths. It is not an Android-device or actual keyboard
test. Production authentication, real provider integration and priority changes
remain out of scope and require separate approval.
