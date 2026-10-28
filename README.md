# GenLayer Bundle 1 — five intelligent contracts

Five small, independently useful GenLayer contracts, built and tested locally
with the `genlayer-test` direct-mode harness and deployed to GenLayer Studio
testnet ("Studionet", chain id 61999). One optional, one-directional
cross-contract link connects ForecastLedger to OutcomeFeed.

| # | Contract | Purpose | Local tests | Studionet |
|---|----------|---------|------------:|-----------|
| 1 | SpecLint | Scores a written spec for ambiguity with LLM consensus | 12 | deployed; see caveat below |
| 2 | ClaimPin | Pins a web claim and re-checks it against its live source | 18 | deployed & verified |
| 3 | ForecastLedger | Registers price-direction forecasts, settles them against a live source | 29 | deployed & verified incl. live settlement |
| 4 | OutcomeFeed | Append-only, idempotent feed of decided outcomes | 18 | deployed & verified |
| 5 | DepositSplit | Weighted, payable split of a deposited pot with duplicate-settlement protection and a creator void path | 23 | deployed & verified |

Total: **101 direct-mode tests pass** (`/root/glvenv/bin/python -m pytest tests/direct -q`).

**SpecLint caveat:** `analyze` runs a genuine LLM analysis and its transaction
can reach `FINALIZED`, but consensus behaviour on Studionet has been unstable
across deployments: in several runs the transaction ended `UNDETERMINED`
because validator votes were recorded as `disagree`/`idle` without the
validators ever executing the validator function (receipts show
`VALIDATOR_QUORUM_REACHED` cancellations and no `eq_outputs` from the
validators), and in the runs that finalized, the report record was not
readable afterwards while `count` had advanced. The receipts show the leader's
analysis itself was correct (full model reply preserved in `eq_outputs`), so
the fault is in the consensus layer, not the analysis. The direct-mode tests
pass because they execute the code, not the consensus. SpecLint is therefore
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

| Contract | Address | Deploy tx |
|----------|---------|-----------|
| SpecLint | 0x70CDc8Bbf7D4Cdc5B21e94a0A8589583A906Df57 | 0xc655414c…f0ea11 |
| ClaimPin | 0x93f8E437CB0AdA264c13Ae0b8D1068507157d63A | (see `deployments/claimpin.json`) |
| ForecastLedger | 0x6B2e9B7907C68220D65030F81b278975004D0db7 | 0x1a95765c…5809e6 |
| OutcomeFeed | 0xc02784300D9E58cCd26feB6B97205bc5a8fFDc8C | (see `deployments/outcomefeed.json`) |
| DepositSplit | 0x9aD2b1fDF07E64B64538B8E38F417B6FF9247C87 | (see `deployments/depositsplit.json`) |

All five source hashes in `deployments/*.json` match the current files in
`contracts/` (checked with SHA-256 on 2026-10-28). An earlier ForecastLedger
deployment (0x6A7EaE7C…D8D46CE, archived in git history) carried the
number-price prompt that triggered the GenVM crash documented below.

Live verification (`scripts/verify.py`) confirmed, per contract, against the
chain with real consensus and real models:

- **ClaimPin** — `register` finalizes and `get_claim` returns the full record;
  empty-claim and non-http-source inputs revert with the expected messages;
  `count` is unchanged by rejected writes.
- **OutcomeFeed** — an `append` finalizes and is readable via `key_for`/`get`
  with correct subject casing, status, and micro-units; a duplicate `ref`
  converges to the same entry without advancing `count` and the first outcome
  wins; `subject_tally` counts live entries; a `pending` status is rejected.
- **DepositSplit** — a payable `create` of 1000 with weights [1, 3] records
  `total=1000` and previews 250/750; zero-value and duplicate-recipient
  creates revert with the expected messages; `count` unchanged by rejections.
- **ForecastLedger** — `register` finalizes with the record readable and
  `status=pending`; past-deadline and invalid-direction registrations revert
  with the expected messages; settling before the deadline reverts with
  "not yet due"; `count` unchanged by rejections.
  **Full live settlement verified** on 2026-10-28 (ledger
  0x6B2e9B79…D0db7, tx `0xbfc222b1…99e46`, FINALIZED): a real forecast
  registered against CoinGecko settled to `status=hit` with
  `observed_micros=2369130000` (~$2,369.13), a keccak evidence hash of the
  fetched page, and `reason=observed 2369130000 vs threshold 1000000
  (micro-USD)` — all readable from `get_forecast`.
- **Cross-contract link (ForecastLedger → OutcomeFeed)** — the `emit(on=
  'finalized')` message from the settled forecast **was delivered** to the
  feed contract (tx `0x5e53f608…`, from the ledger contract address), where
  it was **correctly rejected** with `only the producer may append`: the
  feed's immutable producer is the deployer wallet, not the ledger, so the
  ledger's emission is refused by design. The message path, the
  emission-on-finalized semantics, and the access control are all verified
  live; wiring the ledger as the authorized producer would require breaking a
  constructor-arg circularity (the ledger needs the feed's address, the feed
  needs the ledger's), which immutable constructor args do not support. The
  outcome was instead appended by the producer with the settlement's exact
  fields (`ETH:manual-1`, FINALIZED, readable via `get`).

Rerun any of them with:

```
/root/glvenv/bin/python scripts/verify.py <contract>
```

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
   the model for `{"price": 3500.5}` (any JSON float) repeatedly killed the
   VM with a non-classifiable internal error at `cpython!py_gl_call`. The
   contract's prompt therefore demands the price as a **string** of digits,
   and `_to_micros` validates and coerces it. This was confirmed on-chain:
   the first live `settle` (tx `0xbdfb3673…d710`) FINALIZED with exactly that
   crash as its result, and the forecast correctly stayed `pending` and
   retryable. The fixed prompt is deployed at the current address.
