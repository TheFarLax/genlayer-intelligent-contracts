# GenLayer Bundle 1 — four intelligent contracts

Four small, independently useful GenLayer contracts, built and tested locally
with the `genlayer-test` direct-mode harness and deployed to GenLayer Studio
testnet ("Studionet", chain id 61999). ForecastLedger publishes its decided
outcomes to OutcomeFeed through a one-shot authorization that resolves the
constructor-address circularity between the two.

| # | Contract | Purpose | Local tests | Studionet |
|---|----------|---------|------------:|-----------|
| 1 | ClaimPin | Pins a web claim and re-checks it against its live source | 18 | deployed & verified |
| 2 | ForecastLedger | Registers price-direction forecasts, settles them against a live source | 29 | deployed & verified incl. live settlement |
| 3 | OutcomeFeed | Append-only, idempotent feed of decided outcomes | 24 | deployed & verified |
| 4 | DepositSplit | Weighted, payable split of a deposited pot with duplicate-settlement protection and a creator void path | 24 | deployed; `create`/`preview` verified live, **claim/void payouts not yet exercised on chain** |

Total: **95 direct-mode tests pass** (`/root/glvenv/bin/python -m pytest tests/direct -q`).

A fifth contract, SpecLint, was built, tested, and deployed but **dropped from
the bundle** after persistent consensus instability on Studionet (transactions
intermittently ending UNDETERMINED with validator votes that never executed
the validator function — a consensus-layer behavior the local harness cannot
reproduce). Its source and history are preserved in `archive/` and git; the
investigation record is kept at `docs/speclint-debugging-notes.md`.

## Layout

```
contracts/           one .py file per contract (GenVM Python)
tests/direct/        pytest suites run through the gltest direct harness
scripts/glnet.py     deploy / read / write helpers bound to Studionet
scripts/verify.py    end-to-end live checks against a deployment
patches/             diffs against gltest 0.29.2 needed by the test harness
deployments/         one JSON per deployment: address, tx, source hash
docs/usage.md        per-contract usage examples: inputs and expected outputs
docs/                historical debugging notes (SpecLint)
archive/             dropped-contract sources, preserved for reference
```

## Setup

```
python3 -m venv /root/glvenv
/root/glvenv/bin/python -m ensurepip                       # pip 25.0.1 here
/root/glvenv/bin/python -m pip install \
    genlayer-test==0.29.2 genlayer-py==0.16.3 eth-account

# The three harness patches are REQUIRED for the tests; see below.
SP=/root/glvenv/lib/python3.12/site-packages
cd $SP && patch -p1 < /root/bundle1/patches/0001-wasi_mock.patch \
          && patch -p1 < /root/bundle1/patches/0002-vm.patch
cd /root/bundle1

genlayer account create --name bundle1-builder
# store its password where glnet.py expects it, 0600:
umask 077 && echo '<password>' > ~/.genlayer/.bundle1-builder.pw
```

The Python version matters for the patch path: this environment is
CPython 3.12.14, so the patched tree is under `python3.12`. On a different
minor version, adjust `SP` accordingly.

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
locally (mock web + LLM). No network or GEN is required.

**The tests depend on three patches to the installed `gltest` package**
(`/root/glvenv/lib/python3.12/site-packages/gltest/direct/`), shipped as
real diffs in `patches/`. A plain `pip install genlayer-test==0.29.2` will
run the suite against a harness that silently returns `None` from nondet
calls. Apply them first:

```
SP=/root/glvenv/lib/python3.12/site-packages
cd $SP && patch -p1 < /root/bundle1/patches/0001-wasi_mock.patch \
          && patch -p1 < /root/bundle1/patches/0002-vm.patch
```

The patches were generated from a pristine 0.29.2 install, verified to apply
cleanly, and reproduce the working files byte-for-byte (SHA-256 match). What
each one fixes — `os.fdopen` interception, draining the `Lazy` returned by
`run_nondet`, and a JSON-text fallback for float mock replies — is explained
in `docs/usage.md` under "Reproducing the test environment".

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
  will authorize the ledger).
- ForecastLedger takes one optional constructor arg: the OutcomeFeed address.
  Omit it for a standalone ledger.
- ClaimPin and DepositSplit take no constructor args.
- DepositSplit's `create` must be called with `value` (GEN attached).

**Wiring ForecastLedger → OutcomeFeed** (resolves the circular dependency):
deploy the feed first (producer = your wallet), then the ledger (constructor
arg = feed address), then call the feed's `authorize(ledger_address)` from the
producer wallet. `authorize` is one-shot: exactly one additional writer, ever,
chosen by the producer. After that the ledger's settlements append to the feed
automatically on `finalized`.

The same steps can be done through the Studio UI (paste the file, set the
constructor args, deploy); `scripts/glnet.py` does exactly that via RPC.

## Verified deployments (Studionet, chain 61999)

| Contract | Address | Deploy tx |
|----------|---------|-----------|
| ClaimPin | 0x93f8E437CB0AdA264c13Ae0b8D1068507157d63A | (see `deployments/claimpin.json`) |
| ForecastLedger | 0x805C35978d7B51C787F0d12CaEF0E069e737dDD7 | 0xa9ceb842…cf3166 |
| OutcomeFeed | 0xe167B9301eC2a59E5448ED65CC4F44573142A626 | 0x75f9f741…38b6d06b |
| DepositSplit | 0x9aD2b1fDF07E64B64538B8E38F417B6FF9247C87 | (see `deployments/depositsplit.json`) |

All source hashes in `deployments/*.json` match the current files in
`contracts/` (checked with SHA-256 on 2026-10-28). Superseded deployments
(earlier OutcomeFeed and ForecastLedger instances, dropped SpecLint) remain in
git history.

Live verification (`scripts/verify.py` + dedicated end-to-end runs) confirmed
per contract, against the chain with real consensus and real models:

- **ClaimPin** — `register` finalizes and `get_claim` returns the full record;
  empty-claim and non-http-source inputs revert with the expected messages;
  `count` is unchanged by rejected writes. Separately (not part of
  `verify.py`, which only covers the deterministic write path), rechecking
  against a live web source produced a correct `holds` verdict with a quoted
  deciding fact and evidence hash, and a correct `undeterminable` for an
  uninformative source. Those recheck runs were one-off live calls, not
  re-runnable by script.
- **ForecastLedger** — `register` finalizes with the record readable and
  `status=pending`; past-deadline and invalid-direction registrations revert
  with the expected messages; settling before the deadline reverts with
  "not yet due". **Full live settlement verified** (tx `0xbfc222b1…99e46`,
  FINALIZED, on the predecessor deployment): a real ETH forecast against
  CoinGecko settled to `status=hit` with `observed_micros=2369130000`
  (~$2,369.13), a keccak evidence hash, and a readable record. The current
  deployment adds the feed wiring on top of that verified behavior.
- **ForecastLedger → OutcomeFeed integration — verified end to end on
  2026-10-28** (ledger 0x805C3597…dDD7, feed 0xe167B930…42A626). Sequence,
  all live: the wallet deployed the feed (producer = wallet), deployed the
  ledger (constructor arg = feed address), and called `authorize(ledger)` —
  tx `0xb5b75f37…a957`, FINALIZED, `get_authorized` returns the ledger
  address. A real ETH forecast (threshold 99,000,000,000 micro-USD, far above
  spot) then registered, its deadline passed, and `settle` finalized (tx
  `0x68f4bebf…acecbc`) with `status=miss`, `observed_micros=2362000000`
  (~$2,362), and an evidence hash. The feed count went **0 → 1 with no manual
  append**: entry `ETH:fc-0` arrived via the ledger's finalized-message
  carrying the exact settlement fields, and `subject_tally('ETH')` reports
  `{hits: 0, misses: 1, settled: 1}`. Idempotency on `ref = forecast_id`
  means a retried settlement converges rather than duplicating.
- **OutcomeFeed standalone** (predecessor deployment 0xc0278430…Dc8C, same
  logic minus `authorize`): an `append` finalizes and is readable via
  `key_for`/`get` with correct subject casing, status, and micro-units; a
  duplicate `ref` converges to the same entry without advancing `count`,
  first outcome wins; `subject_tally` counts live entries; a `pending` status
  is rejected with `count` unchanged. The `authorize` write itself is
  additionally covered by 6 direct tests (one-shot, producer-only, bad-writer
  rejection, authorized writer appends, everyone else still refused).
- **DepositSplit** — a payable `create` of 1000 with weights [1, 3] records
  `total=1000` and previews 250/750; zero-value and duplicate-recipient
  creates revert with the expected messages; `count` unchanged by rejections.
  **Not verified live:** `claim` (the `emit_transfer` payout), split closing,
  and the `void` refund. Those paths are covered only by direct-mode tests —
  the transfer leg has never been exercised against Studionet. See
  `docs/usage.md` for the exact list of unverified items.

Rerun any of them with:

```
/root/glvenv/bin/python scripts/verify.py <contract>
```

All four verifiers were run again on 2026-10-28 against the current
deployments and reported `ALL CHECKS PASSED`:
`claimpin`, `forecastledger`, `outcomefeed`, `depositsplit`. These runs are
write-side smoke tests (they add records), so the live counts grow with each
rerun; that is expected, not drift. One fix was needed in `verify.py` for
these runs: the Studionet transaction index lags finalization, so
`_expect_revert` now polls for the transaction record instead of reading it
once. Without the poll it intermittently reported `(no result)` even though
the revert was recorded correctly on chain — a verifier timing bug, not a
contract defect.

## Interacting with each contract

All contracts are plain GenVM contracts; any GenLayer client can call them.
Views: `count`, plus per-contract getters (`get_claim`, `get_forecast`,
`get`, `get_split`, `subject_tally`, `get_record`, `get_producer`,
`get_authorized`, …). Writes: `register`/`recheck`, `register`/`settle`,
`append`/`authorize`, `create`/`claim`/`void`.

**`docs/usage.md` has a worked example per contract**: exact argument lists
with realistic values, the expected view output after each call, and the
exact revert message for each rejected input. It also carries the
verified-vs-unverified table and the harness reproduction steps.

## Design notes

- **No floats in calldata or storage.** Prices travel as integer micro-units
  (3000.50 USD = `3000500000`); scores and payouts are integers. LLM price
  replies are demanded as strings and coerced in `_to_micros`.
- **Deterministic writes only after nondet.** All storage writes and
  cross-contract emissions sit outside `run_nondet_unsafe` blocks, which GenVM
  enforces by aborting the transaction otherwise.
- **Idempotent feed.** OutcomeFeed deduplicates on `ref`; a retried settlement
  converges to the same entry instead of double-counting. The
  ForecastLedger → OutcomeFeed message is emitted on `finalized` only, and a
  feed failure is deliberately swallowed so it cannot strand an
  already-settled forecast.
- **One-shot authorization.** The feed's second writer is granted once, by the
  producer, to a chosen address. This keeps producer-only access control
  (no open append) while breaking the deploy-order circularity.
- **Settlement is retryable, never silent.** If a source is unreadable at the
  deadline, `settle` reverts with `source unavailable` and the forecast stays
  PENDING rather than being settled on a guess.
- **DepositSplit accounting.** Payouts are floored (`total * weight //
  total_weight`), so the sum of payouts never exceeds the pot; leftover dust
  stays in the contract rather than depending on claim order. The `paid` flag
  is written before the transfer in the same transaction, so a failed
  transfer rolls it back. `void` refunds only while no share has been paid.
- **Views report absence instead of reverting** where an RPC path would
  otherwise turn the revert into an opaque "execution failed".

## Known limitations

1. **`sim_fundAccount` never credited** the dedicated wallet
   (0x05C55d…) despite reporting FINALIZED. The wallet was funded manually
   through the faucet instead.
2. **The test harness needs three patches** to the installed gltest package,
   provided in `patches/` and explained in `docs/usage.md`: (a) durable
   `os.fdopen` interception so mock LLM/web replies reach the SDK, (b)
   draining the `Lazy` returned by `run_nondet` before calldata encoding,
   (c) a JSON-text fallback when a mock reply contains floats, which calldata
   cannot encode. Without them the direct harness silently returns `None`
   from nondet calls. They affect only the local test environment, not the
   chain.
3. **Intermittent environment faults**: DNS failures for
   `studio.genlayer.com` and connection resets interrupted some sessions; all
   helpers retry transient network errors but never mask real ones.
4. **Decimal JSON crashes GenVM** (confirmed on-chain, tx `0xbdfb3673…d710`):
   a model reply containing any JSON float kills the VM with a
   non-classifiable internal error at `cpython!py_gl_call`. The ledger's
   prompt therefore demands the price as a **string** and `_to_micros`
   validates and coerces it.
5. **Consensus is slower than the SDK's default poll window.** A settlement
   can sit in `ACCEPTED` past the 2-second wait and still finalize; treat
   "timed out waiting" as "still processing" and re-check the status before
   concluding anything.
6. **DepositSplit payouts are floored**, so a split only reaches `finalized`
   when the floors sum to `total`. `total=1000, weights=[1,3]` distributes
   exactly; `total=1000, weights=[1,1,1]` leaves 1 wei in the contract and
   the split stays `open` forever (and cannot be voided once a share is
   paid). Real amounts in micro-units make this negligible, but it is a
   genuine edge case.
7. **ClaimPin's cooldown and sticky-`broken` behavior** are covered by direct
   tests only; they were not separately exercised against Studionet.
