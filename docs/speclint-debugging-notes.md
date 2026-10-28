# SpecLint on-chain debugging notes

SpecLint was the only contract in this bundle whose write path behaved
differently on Studionet than in the direct-mode test harness. These notes
record what was actually observed, because a future change to the contract or
to GenVM releases may invalidate the workaround.

## Symptom

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
