# SpecLint on-chain debugging notes

SpecLint was the only contract in this bundle whose write path behaved
differently on Studionet than in the direct-mode test harness. These notes
record what was actually observed, because a future change to the contract or
to GenVM releases may invalidate the workaround.

## The decisive evidence: UNDETERMINED receipts

The transaction record for a failed `analyze` (tx
`0xaaef9385…2e9599`, deployment `0xA899…c7e`) shows the real mechanism:

- The **leader** executed successfully. Its `eq_outputs` contained the model's
  full, correct reply:
  `{"acceptance":25,"ambiguities":[],"ambiguous":false,"assumptions":[],
  "clarity_score":100,"constraints":25,"deliverables":25,"scope":25,"summary":""}`
  and its final `result` was the report id `"48eaabd7bd-48eaab"`.
- Three validators voted **disagree** and two **idle**, so the transaction
  ended **UNDETERMINED** and its writes were discarded — which is exactly the
  observed symptom: `analyze` "succeeded" from the leader's perspective, the
  caller saw a result, but `get_report` found nothing because the transaction
  never finalized.
- None of the disagreeing validators produced `eq_outputs` of their own; the
  idle ones show `CONSENSUS_VALIDATOR_QUORUM_REACHED` cancellations. So the
  validator function (the `_validator_core` comparison) apparently did not run
  as written, or its failure was reported as a plain "disagree" vote.

Two consequences follow:

1. The "storage write disappears" theory was wrong in an important way: the
   writes were discarded because the transaction was UNDETERMINED, not because
   a finalized write went missing. A FINALIZED run on the older
   counter-based id scheme (deployment `0x70CD…Df57`) did leave `count == 1`
   and the report record — but with a *counter-derived id* whose `count`
   itself is unreliable for lookup, so reads still needed to guess the id.
2. Any exception inside a validator is reported to consensus as "disagree"
   (`run_nondet_unsafe` documents that validator errors are equivalent to
   returning `False`). The validator now wraps its entire body in
   `try/except` → explicit `False`, so only genuine disagreement rejects.

## Why the direct tests could not catch this

The direct-mode harness runs the code, not the consensus. There is no
multi-node vote, no commit-reveal, no quorum cancellation — `run_nondet`
executes leader and validator once and compares locally. That is why 12/12
tests pass while the chain ends UNDETERMINED. This gap is inherent to the
harness, not a bug in the tests.

## Earlier symptom (kept for the record)

`analyze` returned a report id and the transaction reached `FINALIZED` with a
successful result, but:

- `count()` returned 1 or 2 (so *some* storage write landed), and
- `get_report(<the returned id>)` returned `found: false`, and
- `get_by_hash(<spec hash>)` returned `""`.

Three separate deployments (`0x3451…019b`, `0x0F8A…45F4`, `0xAc71…27f3`)
reproduced it.

## What was ruled out

Each of these was deployed and exercised on Studionet with a real model:

| Probe | Result |
| --- | --- |
| Plain `TreeMap[str, str]` write from a hardcoded key | persists |
| `@allow_storage` dataclass written to a literal key | persists |
| SpecLint's exact storage shape (dataclass map + index map + counter) written deterministically | persists |
| `run_nondet_unsafe` with a JSON prompt, no storage write | succeeds |
| `run_nondet_unsafe`, then a string map write, then a dataclass write | persists |
| The whole SpecLint contract with `analyze`, instrumentation removed | **fails** |

So neither nondeterministic execution nor dataclass storage is the cause on its
own. The failure is specific to something in `analyze`'s combination.

## What the instrumentation showed

A copy of the contract was deployed with a single extra statement immediately
after `run_nondet_unsafe`:

```python
self.by_hash["__probe__"] = json.dumps(result, sort_keys=True)[:900]
```

That write **did** persist, and reading it back returned the real model reply,
correctly shaped:

```json
{"acceptance": 20, "ambiguous": false, "assumptions": [...],
 "clarity_score": 90, "constraints": 25, "deliverables": 25,
 "scope": 25, ... }
```

One detail is worth recording: the persisted string was exactly 900 characters
and ended mid-word (`"scope`). The model's reply contained a value whose
rendered form was long, and truncating the snapshot to 900 characters is what
cut it. That is a property of the snapshot expression, not of storage.

The failure therefore occurs *after* the nondet block returns a good dict and
*after* the first post-nondet string write succeeds.

## Consequence for the design

The contract was rewritten so that:

1. The report id is derived from content — `spec_hash` plus the sender — rather
   than from a storage-backed counter. A caller can recompute the id without
   trusting the write, and a repeated submission is idempotent.
2. Every stored field is a plain scalar computed before the single assignment.
   `total` is no longer a second expression over a dict lookup; it is the same
   integer as `clarity_score`, so there is no chance of a partially-computed
   record.
3. `get_report` reports `found: false` instead of reverting, so an unreadable
   record is distinguishable from an RPC failure.

## Status

This diagnosis is **incomplete**. The exact GenVM or Studionet behaviour that
discards the dataclass write is not identified. Treat the SpecLint deployment
records in `deployments/speclint.json` as the authority on whether a given
build was verified end to end, and re-run the verification script after any
change to this file. Do not describe SpecLint as deployed-and-verified based on
the 100 passing direct-mode tests alone.
