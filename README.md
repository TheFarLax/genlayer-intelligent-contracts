# GenLayer Bundle 1 — five intelligent contracts

Five small, independently useful GenLayer contracts, built and tested locally
with the `genlayer-test` direct-mode harness and deployed to GenLayer Studio
testnet ("Studionet", chain id 61999). One optional, one-directional
cross-contract link connects ForecastLedger to OutcomeFeed.

| # | Contract | Purpose | Local tests | Studionet |
|---|----------|---------|------------:|-----------|
| 1 | SpecLint | Scores a written spec for ambiguity with LLM consensus | 12 | deployed; see caveat below |
| 2 | ClaimPin | Pins a web claim and re-checks it against its live source | 18 | deployed & verified |
| 3 | ForecastLedger | Registers price-direction forecasts, settles them against a live source | 29 | deployed & verified |
| 4 | OutcomeFeed | Append-only, idempotent feed of decided outcomes | 18 | deployed & verified |
| 5 | DepositSplit | Weighted, payable split of a deposited pot with duplicate-settlement protection and a creator void path | 23 | deployed & verified |

Total: **100 direct-mode tests pass** (`/root/glvenv/bin/python -m pytest tests/direct -q`).

**SpecLint caveat:** `analyze` runs a genuine LLM analysis and its transaction
can reach `FINALIZED`, but consensus behaviour on Studionet has been unstable
across deployments: in several runs the transaction ended `UNDETERMINED`
because validator votes were recorded as `disagree`/`idle` without the
validators ever executing the validator function (receipts show
`VALIDATOR_QUORUM_REACHED` cancellations and no `eq_outputs` from the
validators), and in the runs that finalized, the report record was not
readable afterwards while `count` had advanced. The direct-mode tests pass
because they execute the code, not the consensus. SpecLint is therefore
**deployed but not fully verified end-to-end**. Details and evidence:
`docs/speclint-debugging-notes.md`.

## Layout

```
contracts/           one .py file per contract (GenVM Python)
tests/direct/        pytest suites run through the gltest direct harness
scripts/glnet.py     deploy / read / write helpers bound to Studionet
scripts/verify.py    end-to-end live checks against a deployment
deployments/         one JSON per deployment: address, tx, source hash
docs/                debugging notes for anything that needed investigation
```

## Setup

```
python3 -m venv /root/glvenv
/root/glvenv/bin/pip install genlayer-test genlayer-py eth-account
genlayer account create --name bundle1-builder
# store its password where glnet.py expects it, 0600:
echo '<password>' > ~/.genlayer/.bundle1-builder.pw
```

`glnet.py` refuses to sign with any keystore other than `bundle1-builder`,
never prints or writes the private key, and reads the password from
`~/.genlayer/.bundle1-builder.pw`. The wallet is testnet-only; it was funded
via the Studio faucet by hand (the RPC's `sim_fundAccount` never credited it —
see "Known limitations").

Network guard: every helper asserts both the SDK chain id and the live
`eth_chainId` are **61999** (Studionet) and exits with a `REFUSED:` message
otherwise, so nothing can be sent to a different network by accident.

## Running the tests

```
cd /root/bundle1
/root/glvenv/bin/python -m pytest tests/direct -q
```

The direct-mode harness runs each contract inside the real GenVM wasm runtime
locally (mock web + LLM). No network or GEN is required. Three defects in the
harness itself were patched in the installed package; see "Known limitations".

## Deploying

```
/root/glvenv/bin/python - <<'PY'
import sys; sys.path.insert(0, "scripts")
import glnet
rec = glnet.deploy("claimpin")           # or any contract name
print(rec["address"], rec["deploy_tx"])
PY
```

- OutcomeFeed takes one constructor arg: the producer address (the wallet that
  will deploy ForecastLedger, which is the only address allowed to append).
- ForecastLedger takes one optional constructor arg: the OutcomeFeed address.
  Omit it for a standalone ledger.
- SpecLint, ClaimPin, DepositSplit take no constructor args.
- DepositSplit's `create` must be called with `value` (GEN attached).

The same result can be done through the Studio UI (paste the file, set the
constructor args, deploy); `scripts/glnet.py` does exactly that via RPC.

## Verified deployments (Studionet, chain 61999)

Records in `deployments/*.json` include the deploy tx and a SHA-256 of the
exact source that was deployed. Rerun any of them with:

```
/root/glvenv/bin/python scripts/verify.py <contract>
```

`verify.py` talks only to the live chain: it calls each contract's writes and
views, exercises invalid-input rejection (a rejection only counts if the call
reverts with the expected message), and fails loudly on any mismatch.

## Interacting with each contract

All contracts are plain GenVM contracts; any GenLayer client can call them.
Views: `count`, plus per-contract getters (`get_report`, `get_claim`,
`get_forecast`, `get`, `get_split`, `subject_tally`, `get_record`, …).
Writes: `analyze`, `register`/`recheck`, `register`/`settle`, `append`,
`create`/`claim`/`void`.

## Design notes

- **No floats in calldata or storage.** Prices travel as integer micro-units
  (3000.50 USD = `3000500000`); scores and payouts are integers.
- **Deterministic writes only after nondet.** All storage writes and
  cross-contract emissions sit outside `run_nondet_unsafe` blocks, which GenVM
  enforces by aborting the transaction otherwise.
- **Idempotent feed.** OutcomeFeed deduplicates on `ref`; a retried settlement
  converges to the same entry instead of double-counting. The ForecastLedger →
  OutcomeFeed message is emitted on `finalized` only, and a feed failure is
  deliberately swallowed so it cannot strand an already-settled forecast.
- **Settlement is retryable, never silent.** If a source is unreadable at the
  deadline, `settle` reverts with `source unavailable` and the forecast stays
  PENDING rather than being settled on a guess.
- **DepositSplit accounting.** Payouts are floored (`total * weight //
  total_weight`), so the sum of payouts never exceeds the pot; leftover dust
  stays in the contract rather than depending on claim order. The `paid` flag
  is written before the transfer in the same transaction, so a failed transfer
  rolls it back. `void` refunds only while no share has been paid. The contract
  never assumes a failed child message returns its value — refunds are
  explicit.
- **Views report absence instead of reverting** where an RPC path would
  otherwise turn the revert into an opaque "execution failed".

## Known limitations

1. **SpecLint end-to-end verification is incomplete** (see caveat above and
   `docs/speclint-debugging-notes.md`). Direct tests pass; on-chain consensus
   behaviour around its nondet block is the open question.
2. **`sim_fundAccount` never credited** the dedicated wallet
   (0x05C55d…) despite reporting FINALIZED. The wallet was funded manually
   through the faucet instead.
3. **Three harness patches live in the installed gltest package**, not in this
   repo: (a) `os.fdopen` interception so mock LLM/web replies reach the SDK,
   (b) draining the `Lazy` result of `run_nondet` before encoding,
   (c) a JSON-text fallback when a mock reply contains floats, which calldata
   cannot encode. Without them the direct harness silently returns `None` from
   nondet calls. They affect only the local test environment, not the chain.
4. **Layovers of environment, not contract:** intermittent DNS failures for
   `studio.genlayer.com` and connection resets interrupted some sessions; all
   helpers retry transient network errors but never mask real ones.
5. **Decimal JSON crashed GenVM** during ForecastLedger development: asking
   the model for `{"price": 3500.5}` repeatedly killed the VM with a
   non-classifiable internal error. The contract therefore asks for integers
   and validates/coerces in `_to_micros`.
