# DepositSplit — archived (2026-10-29)

DepositSplit was removed from the active bundle after a live payout defect
could not be resolved within a time-boxed investigation.

## What is archived here

| File | What it is |
|------|------------|
| `depositsplit.py` | final source, with the rounding fix applied |
| `test_depositsplit.py` | 35 direct-mode tests, all passing |
| `deployments-depositsplit.json` | v2 deployment record (apportionment fix) |
| `deployments-depositsplit.v1.json` | v1 deployment record (flooring bug) |
| `audit_depositsplit.py` | read-only live audit of the recorded evidence |

The tests are no longer collected by `pytest tests/direct` because they are
no longer under `tests/`. They still run standalone from the repo root; their
`direct_deploy` paths were repointed at `archive/depositsplit/depositsplit.py`:

```
/root/glvenv/bin/python -m pytest archive/depositsplit/test_depositsplit.py -q
```

The audit script still reads `deployments/depositsplit.json`, so run it only
after restoring that record to `deployments/` (or point it at the archived
copy).

## Deployed instances — still on chain, not deleted

- v2 (final, with rounding fix): `0x1D2202D5a55f2DA6Fd51BA32503866E60E83d315`
  deploy tx `0x12d9fd6899ec98d5988edb99110af20983d266f67770c386cdcff1562109ef62`
- v1 (flooring bug): `0x9aD2b1fDF07E64B64538B8E38F417B6FF9247C87`

Both remain on Studionet and hold test funds. Neither is modified or removed.

## Defect 1 — rounding (FIXED, verified on chain)

`_share_amount` used `total * weight // total_weight`, stranding up to
`recipients - 1` units. `claim` only finalized at `released >= total`, which
such a pot could never reach, and `void` refused once any share was paid. The
money was therefore unreachable.

Fixed with largest-remainder apportionment (`_apportion`): floor each share,
then give the leftover units to the largest remainders, ties broken by lower
index. Shares sum to exactly the pot and are independent of claim order.

Verified live: split `sp-0` (1000 across three equal weights) apportioned to
**334/333/333**, released 1000, and reached `finalized`. `sp-1` paid 500 and
was then voided with the 500 remainder refunded.

## Defect 2 — payouts never arrive (UNRESOLVED — reason for archiving)

`claim` and `void` mark the share paid, update `released`, and finalize the
split, but the value never reaches the recipient.

`emit_transfer` posts a message to the recipient. For an address with no
contract code the message finalizes with:

```
Contract 0x… not found
value_credited: false
```

Verified across five transfers (334, 333, 333, 500, 500) to three fresh
externally-owned addresses and to the creator wallet itself. Recipient
balances stayed 0 and the contract balance never fell; the funds stayed in
the contract.

### The contract-recipient experiment (inconclusive)

Attempted the supported shape — a recipient that is a deployed contract with
`__receive__`. A minimal sink was deployed and bound to the split contract.

- Cross-contract **view** calls work from such a sink: `.view().count()`
  returned the correct live value (`view=10`).
- Cross-contract **write/emit** to `claim` did not work. The message executes
  with the sink as sender (`from 0xB9a4… to 0x1D22…`), but returns
  `exit_code 1`, the split stays `open`, and the sink receives nothing.
- The receipt exposes only `result: 6 / MAJORITY_AGREE`; no readable error
  text was available from `eth_getTransactionByHash` (`leader_receipt` holds
  only `exit_code 1`).

Earlier attempts also failed from my own API mistakes — calling `.claim()`
directly instead of `.emit(on="finalized").claim()`, and not using
`.view()` for reads. Those were corrected. The remaining `exit_code 1` on a
correctly-formed emit was not explained within the time box.

**This is not proof that contract-to-contract transfers are impossible.**
The ForecastLedger → OutcomeFeed `emit(on="finalized").append(...)` path is
proven working on the same chain, so contract-to-contract emits do work in
general. What was not established is why this particular value-transfer emit
fails. Resolving it needs a working value-receiving reference contract to
compare against, which was not available.

## Why the local tests did not catch it

With no `_gl_call_hook` installed, the gltest direct harness returns `None`
for any cross-contract request and never checks that the target exists. All
35 DepositSplit tests, including payout assertions, therefore passed against
behaviour that cannot work on chain. Direct-mode tests must not be treated as
evidence for any value-transfer path.

## If this is picked up again

1. Find a known-good contract on Studionet that receives native value in
   `__receive__`, and confirm a transfer into it succeeds.
2. Compare its `emit_transfer` call and `__receive__` signature against this
   one.
3. Only then decide between contract-recipient payouts and a documented pull
   ledger. A pull ledger records what is owed without claiming value moved,
   and must be described as such.
