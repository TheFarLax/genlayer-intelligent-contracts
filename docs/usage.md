# Usage examples

Every example below uses the **deployed Studionet instances** (chain 61999).
Inputs are the exact argument lists passed over RPC; outputs are the shapes
the deployed views actually return.

Two conventions apply throughout:

- **Money is integer micro-units.** GenLayer calldata has no float type, so
  USD amounts are carried as `micros` (1e-6). `3000.50 USD` is
  `3000500000`. GEN values are wei.
- **Timestamps are absolute unix seconds.**

A quick way to run one call by hand:

```python
# /root/glvenv/bin/python
import sys; sys.path.insert(0, "/root/bundle1/scripts")
import glnet
glnet.read("0x93f8E437CB0AdA264c13Ae0b8D1068507157d63A", "count")
```

---

## ClaimPin — `0x93f8E437CB0AdA264c13Ae0b8D1068507157d63A`

Pins a text claim together with the URL that should support it, then
re-checks it against that live source on an interval.

### `register(claim, source_url, criteria, interval_secs) -> claim_id`

| Input | Example |
|-------|---------|
| `claim` | `"The Eiffel Tower is 330 metres tall."` |
| `source_url` | `"https://en.wikipedia.org/wiki/Eiffel_Tower"` |
| `criteria` | `"Does the page state the tower's height as 330 m?"` |
| `interval_secs` | `3600` (minimum 60) |

```python
glnet.write(CLAIMPIN, "register", [
    "The Eiffel Tower is 330 metres tall.",
    "https://en.wikipedia.org/wiki/Eiffel_Tower",
    "Does the page state the tower's height as 330 m?",
    3600,
])
# -> "claim-0"
```

`register` is deterministic: it only stores the claim. IDs are
`claim-{n}`.

**Expected view output immediately after `register`** — note
`verdict = "unchecked"`, `check_count = 0`, `last_checked_at = 0`:

```json
{
  "found": true, "claim_id": "claim-0",
  "owner": "0x05C55d0d2Ba7fe495492849fEca1BF2bBF939a4C",
  "claim": "The Eiffel Tower is 330 metres tall.",
  "source_url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
  "criteria": "Does the page state the tower's height as 330 m?",
  "interval_secs": 3600, "created_at": 1793188514,
  "last_checked_at": 0, "check_count": 0,
  "verdict": "unchecked", "reason": "", "evidence_hash": "",
  "broken_at": 0, "has_broken": false
}
```

### `recheck(claim_id) -> verdict`

Runs the nondeterministic check (fetch + LLM verdict) with validator
agreement in the same call.

```python
glnet.write(CLAIMPIN, "recheck", ["claim-0"])   # -> "holds"
```

Verdicts: `holds`, `broken`, `undeterminable`, `unchecked`.

**Expected output** on a successful recheck: `verdict` becomes `"holds"` (or
`"broken"`/`"undeterminable"`), `check_count` increments, `last_checked_at` is
set, `reason` carries the deciding sentence, and `evidence_hash` is a keccak
digest of the fetched text. Once `broken`, `has_broken` stays `true` and
`broken_at` records when — a later `holds` does not clear it.

**Observed live**: a correct `holds` verdict with a quoted deciding fact and
an evidence hash; a correct `undeterminable` for an uninformative source.

### Rejections (verified live)

| Call | Error message |
|------|---------------|
| `register("", url, crit, 60)` | `claim is required` |
| `register(claim, "ftp://x", crit, 60)` | `source_url must be http(s)` |
| `register(claim, url, crit, 30)` | `interval_secs must be >= 60` |
| `recheck("claim-0")` twice inside 60 s | `recheck too soon` |
| `recheck("nope")` | `claim not found` |

`count()` is unchanged by any rejected write.
`get_claim("nope")` returns `{"found": false, "claim_id": "nope"}` rather
than reverting.

---

## ForecastLedger — `0x805C35978d7B51C787F0d12CaEF0E069e737dDD7`

Registers a public price-direction forecast and settles it against a live
source once its deadline passes. Wired to OutcomeFeed.

### `register(asset, direction, threshold_micros, deadline, source_url, note) -> forecast_id`

| Input | Example |
|-------|---------|
| `asset` | `"ETH"` |
| `direction` | `"up"` (or `"down"`) |
| `threshold_micros` | `2362500000` (= $2,362.50) |
| `deadline` | `1793188514` (absolute unix seconds, must be future) |
| `source_url` | `"https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"` |
| `note` | `"ETH above 2362.50 by the deadline"` |

```python
glnet.write(LEDGER, "register", [
    "ETH", "up", 2362500000, 1793188514,
    "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
    "ETH above 2362.50 by the deadline",
])
# -> "fc-0"
```

**Expected view output right after `register`**:

```json
{
  "found": true, "forecast_id": "fc-0",
  "caller": "0x05C55d0d2Ba7fe495492849fEca1BF2bBF939a4C",
  "asset": "ETH", "direction": "up",
  "threshold_micros": 2362500000, "deadline": 1793188514,
  "source_url": "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
  "note": "ETH above 2362.50 by the deadline",
  "status": "pending", "observed_micros": null,
  "settled_at": 0, "evidence_hash": "", "reason": ""
}
```

### `settle(forecast_id) -> status`

```python
glnet.write(LEDGER, "settle", ["fc-0"])   # -> "hit" | "miss"
```

Resolves to `hit` when the observed price satisfies the direction against the
threshold, else `miss`. If the source is unreachable, `settle` **reverts with
`source unavailable`** and the forecast stays `pending` — it is retryable and
never guessed.

**Expected output after a live settle** (real run, tx `0x68f4bebf…acecbc`,
FINALIZED; threshold deliberately set to 99,000,000,000 so it must miss):

```json
{
  "status": "miss", "observed_micros": 2362000000,
  "evidence_hash": "0ba695d5711d699cacc6f2a9bda2ceb18341842f305d8ee959e1c0924ad1ab2f",
  "reason": "observed 2362000000 vs threshold 99000000000 (micro-USD)"
}
```

### Rejections (verified live)

| Call | Error message |
|------|---------------|
| `register("", ...)` | `asset is required` |
| `register("ETH", "sideways", ...)` | `direction must be up or down` |
| `register(..., deadline in the past, ...)` | `deadline must be in the future` |
| `settle("fc-0")` before the deadline | `not yet due` |
| `settle("fc-0")` twice | `already settled` |
| `settle("nope")` | `forecast not found` |

`get_record(caller)` aggregates the caller's settled forecasts:

```json
{"caller": "0x05C5...9a4C", "hits": 0, "misses": 1, "settled": 1, "hit_rate_bps": 0}
```

---

## OutcomeFeed — `0xe167B9301eC2a59E5448ED65CC4F44573142A626`

Append-only, idempotent record of decided outcomes. Normally written by the
ledger; the producer wallet may also append directly.

### `append(ref, subject, status, observed_micros, evidence_hash, reason) -> key`

| Input | Example |
|-------|---------|
| `ref` | `"fc-0"` (idempotency key, unique per producer) |
| `subject` | `"ETH"` (upper-cased on write) |
| `status` | `"hit"` or `"miss"` only |
| `observed_micros` | `3100000000` |
| `evidence_hash` | `"a1b2"` |
| `reason` | `"observed above threshold"` |

```python
glnet.write(FEED, "append", ["fc-7", "eth", "hit", 3100000000, "a1b2", "observed above threshold"])
# -> "ETH:fc-7"
```

**Expected view output after that append**:

```json
{"found": true, "key": "ETH:fc-7", "seq": 1,
 "producer": "0x05C55d0d2Ba7fe495492849fEca1BF2bBF939a4C",
 "ref": "fc-7", "subject": "ETH", "status": "hit",
 "observed_micros": 3100000000, "evidence_hash": "a1b2",
 "reason": "observed above threshold", "appended_at": 1793188596}
```

**Idempotency**: a second `append` with the same `ref` returns the *existing*
key and writes nothing — first outcome wins, `count` does not advance:

```python
glnet.write(FEED, "append", ["fc-7", "BTC", "miss", 1, "", ""])   # -> "ETH:fc-7"
glnet.read(FEED, "count")                                         # unchanged
```

**Tally**:

```python
glnet.read(FEED, "subject_tally", ["ETH"])
# {"subject": "ETH", "hits": 0, "misses": 1, "settled": 1, "hit_rate_bps": 0}
```

### `authorize(writer) -> true`

Producer-only, one-shot. Used once to let the ledger append:

```python
glnet.write(FEED, "authorize", [LEDGER])   # tx 0xb5b75f37…a957, FINALIZED
glnet.read(FEED, "get_authorized")          # -> "0x805C35978d7B51C787F0d12CaEF0E069e737dDD7"
```

A second call reverts with `an authorized writer already exists`.

### Rejections (verified live)

| Call | Error message |
|------|---------------|
| `append(..., status="pending", ...)` | `status must be hit or miss` |
| `append("", ...)` | `ref is required` |
| caller is not producer/authorized | `only the producer may append` |
| `authorize` from non-producer | `only the producer may authorize` |

`get("nope")` returns `{"found": false, "key": "nope"}`;
`key_for("nope")` returns `""`.

---

## DepositSplit — `0x1D2202D5a55f2DA6Fd51BA32503866E60E83d315`

Splits a deposited GEN pot across weighted recipients; each recipient claims
their own share once.

Supersedes `0x9aD2b1fDF07E64B64538B8E38F417B6FF9247C87`, which had the
flooring bug described under "Rounding" below; that instance is unchanged and
recorded in `deployments/depositsplit.v1.json`.

> **CRITICAL, verified on chain 2026-10-28: `claim` and `void` cannot move
> value to a normal wallet.** GenLayer delivers the payout as a message to
> the recipient address, and an address with no contract code fails with
> `Contract 0x… not found`; `value_credited` is `false` and the recipient's
> balance does not change. The contract's own balance is also unchanged, so
> the funds stay in the contract. This was measured with three fresh EOA
> recipients and with the creator wallet itself:
>
> | Attempt | Tx | Result |
> |---|---|---|
> | claim 334 to EOA | `0x4a76cbcd…` | `Contract 0xE1b9… not found`, credited false |
> | claim 500 to EOA | `0xc0463527…` | `Contract 0xE1b9… not found`, credited false |
> | void refund 500 to creator | `0x97f80f95…` | `Contract 0x05C5… not found`, credited false |
> | claim 500 to creator wallet | `0x34ec986a…` | `Contract 0x05C5… not found`, credited false |
>
> The *accounting* is correct and finalized in every case: shares are marked
> paid, `released` tracks `paid_amount`, and the split reaches `finalized`.
> Only the outward transfer fails. A recipient that is a deployed GenLayer
> contract implementing `__receive__` is the supported shape; that half was
> not proven end to end before this handoff stopped.

> **State of the new instance:** splits `sp-0` (three equal weights, 1000,
> finalized as 334/333/333) and `sp-1` (500 paid then voided) were created and
> finalized on chain; their payouts are the failed transfers listed above.
> The contract holds those funds.

### `create(title, recipients, weights) -> split_id`  (payable)

| Input | Example |
|-------|---------|
| `title` | `"Team payout"` |
| `recipients` | `["0x…a11", "0x…b22"]` |
| `weights` | `[1, 3]` |
| `value` | `1000` wei |

```python
glnet.write(DEPOSIT, "create", ["Team payout", [A, B], [1, 3]], value=1000)
# -> "sp-0"
```

**Expected view output after `create`**:

```json
{"found": true, "split_id": "sp-0",
 "creator": "0x05C55d0d2Ba7fe495492849fEca1BF2bBF939a4C",
 "title": "Team payout", "total": 1000, "released": 0,
 "recipient_count": 2, "status": "open",
 "created_at": 1793188514, "closed_at": 0}
```

```python
glnet.read(DEPOSIT, "preview", ["sp-0", 0])   # -> 250
glnet.read(DEPOSIT, "preview", ["sp-0", 1])   # -> 750
```

### `claim(split_id) -> amount`

```python
glnet.write(DEPOSIT, "claim", ["sp-0"])   # as recipient A -> 250
```

Payouts use largest-remainder apportionment (see "Rounding" below), so the
shares always sum to exactly `total`. The `paid` flag is written before the
transfer in the same transaction, so a failed transfer rolls the flag back
with it. The split moves to `status: "finalized"` when no share is left
unpaid.

**Verified live:** the accounting (share amounts, `paid`, `released`,
`finalized`) is correct. The outward transfer is not — see the CRITICAL note
at the top of this section. Reverts: `split not found`, `split is not open`,
`caller is not a recipient`, `already paid`.

### Rounding

Plain floor division (`total * weight // total_weight`) strands up to
`recipients - 1` units. Those units were unrecoverable: the split never
reached `finalized`, and `void` refused once a share had been paid. Measured
before the fix: a 1000 pot over three equal weights paid 333+333+333, left
999 of 1000 released, and stayed `open` forever.

The fix is largest-remainder (Hamilton) apportionment:

1. give every share its floor,
2. distribute the leftover units one each to the largest remainders,
3. break remainder ties by lower index.

Every share lands within `[floor, ceil]` of its exact quota, and the shares
sum to exactly `total`. The tie-break is a fixed function of the index, so
the result never depends on claim order. Verified live: `sp-0` previews
`[334, 333, 333]` and finalized with exactly those amounts.

### `void(split_id) -> "void"`

```python
glnet.write(DEPOSIT, "void", ["sp-0"])   # as the creator
```

Creator-only, and allowed even after a partial payout: it refunds the
**unclaimed remainder** (`total - released`), not the whole pot, so paid
shares are never refunded twice. Marks the split `void` and sets
`released = total`. A zero remainder emits nothing.

Refusing after a payout (the old behaviour) trapped funds for recipients who
never claimed, so this was changed. Reverts: `split not found`,
`split is not open`, `only the creator may void`.

**Verified live:** the remainder arithmetic and the `void` status are
correct (`sp-1`: 500 claimed, remainder 500). The refund transfer itself
failed to reach the creator wallet — same `Contract … not found` cause as
`claim`.

### Rejections (verified live)

| Call | Error message |
|------|---------------|
| `create("t", [A, B], [1, 3])` with value 0 | `deposit must be positive` |
| `create("t", [A, A], [1, 1])` with value 10 | `duplicate recipient at index 1` |
| `create("", ...)` with value 10 | `title is required` |
| `create("t", [A], [1, 2])` with value 10 | `recipients and weights must be the same length` |
| `create("t", [A], [0])` with value 10 | `weight must be positive at index 0` |

`count()` is unchanged by rejected creates.

---

## What was verified where

This is the distinction that matters for judging the bundle. "Direct test"
means the local gltest harness (real GenVM wasm, mocked web/LLM, **single
validator**). "Live" means against Studionet with real consensus and real
models.

| Behaviour | Direct test | Live on Studionet |
|-----------|:-----------:|:-----------------:|
| ClaimPin `register` + all validations | yes | **yes** |
| ClaimPin `recheck` verdict + evidence | yes | **yes** |
| ClaimPin cooldown / sticky `broken` | yes | — (only direct) |
| ForecastLedger `register` + validations | yes | **yes** |
| ForecastLedger `settle` hit/miss + `unavailable` revert | yes | **yes (hit and miss observed)** |
| ForecastLedger → OutcomeFeed finalized message | yes (hook capture) | **yes (end-to-end, count 0→1)** |
| OutcomeFeed `append`, idempotency, tally, rejections | yes | **yes** (current + predecessor deployment) |
| OutcomeFeed `authorize` one-shot semantics | yes | **yes** |
| DepositSplit `create` + preview + amount checks | yes | **yes** |
| DepositSplit `create` rejections | yes | **yes** |
| DepositSplit remainder apportionment (sums to pot) | yes | **yes** (`sp-0` = 334/333/333) |
| DepositSplit split closing after last claim | yes | **yes** (`sp-0` finalized) |
| DepositSplit `void` after partial payout (remainder) | yes | **yes** (`sp-1`, remainder 500) |
| **DepositSplit `claim` value reaching a wallet** | yes (unrealistically) | **NO — fails: "Contract … not found"** |
| **DepositSplit `void` refund reaching a wallet** | yes (unrealistically) | **NO — fails the same way** |
| **DepositSplit payout to a contract recipient** | not modelled | **NOT PROVEN** (fixture attempt stopped) |

### The DepositSplit caveat, stated plainly

DepositSplit is a **working ledger of who is owed what** with a **broken
payout leg for wallet recipients**. Concretely, verified on chain:

- The accounting is correct: shares are apportioned so they sum to the pot,
  `paid`/`paid_amount` are recorded, and the split finalizes.
- The transfer does not work for a plain address. Every attempt produced a
  FINALIZED message whose result was `Contract 0x… not found` with
  `value_credited: false`. Recipient balances stayed 0 and the contract's
  balance was unchanged, so the funds sit in the contract.

Why the local tests did not catch it: the gltest direct harness, with no
`_gl_call_hook` installed, returns `None` for any cross-contract request and
never checks whether the target contract exists. It therefore reported 31
passing DepositSplit tests, including payout assertions, against behaviour
that cannot work on chain.

What is still unproven:

1. That a recipient which *is* a deployed GenLayer contract implementing
   `__receive__` receives the value. This is the supported shape per the SDK
   (`emit_transfer` docs say the receiver catches it in `__receive__`), and
   cross-contract *view* calls were proven working on chain, but the
   value-transfer half was not demonstrated before this work stopped.
2. Whether any mechanism exists in this GenVM version to pay an
   externally-owned wallet at all.

Treat the deployed instance as unsafe for real payouts until (1) is shown.
The contract holds the test deposits; `glnet.balance_wei(address)` versus
`get_split(sid)["released"]` shows what an attempt moved (nothing).

---

## Reproducing the test environment

The 106 direct-mode tests need **three patches to the installed `gltest`
package**. They are shipped here as real diffs in `patches/`, generated
against a pristine `genlayer-test==0.29.2`, and verified to apply cleanly and
reproduce the working tree byte-for-byte.

```bash
python3 -m venv /root/glvenv
/root/glvenv/bin/python -m ensurepip            # or use uv
/root/glvenv/bin/python -m pip install genlayer-test==0.29.2 genlayer-py==0.16.3 eth-account

SP=/root/glvenv/lib/python3.12/site-packages
cd $SP
patch -p1 < /root/bundle1/patches/0001-wasi_mock.patch
patch -p1 < /root/bundle1/patches/0002-vm.patch

cd /root/bundle1
/root/glvenv/bin/python -m pytest tests/direct -q     # 106 passed
```

### What the three patches fix, and why they are needed

All three defects are in the **test harness**, not in this repo's contracts.
Without them the direct VM silently returns `None` from nondeterministic
calls, so tests would appear to pass vacuously or fail confusingly.

1. **`os.fdopen` interception is not durable** (`wasi_mock.py`,
   `_self_patch_fdopen`, plus its call in `vm.py`).
   The harness patches `os.fdopen` with `unittest.mock.patch`, but the SDK's
   fake layer rebinds `os.fdopen` afterwards, restoring the real one. Contract
   code then reaches `os.fdopen(fd, "rb")` in the SDK's `gl_call_generic`
   with a `BytesIO` instead of a descriptor, every nondet read comes back
   empty, and the `Lazy` resolves to `None`. Fix: rebind `os.fdopen` on the
   module itself for the whole session.

2. **The leader's `Lazy` is not drained** (`wasi_mock.py`, in the
   `run_nondet` wrapper).
   The SDK's non-det I/O returns lazily; production GenVM drains the wrapper
   inside the sub-VM. Direct mode encoded the wrapper object itself, so
   callers saw a `Lazy` instead of the decoded value. Fix: call `.get()` on
   a `Lazy` result before calldata-encoding it.

3. **Float mock replies cannot be calldata-encoded** (`wasi_mock.py`,
   `_json_text` + the fallback in `gl_call`).
   Calldata has no float type, so a mocked LLM reply such as
   `{"price": 3500.12}` made `calldata.encode` raise and the call return
   `2**32 - 1`. Real GenVM hands JSON text back over the ABI, so the patch
   does the same: if encoding fails, the inner payload degrades to JSON text
   inside the unchanged `{"ok": ...}` envelope.

This mirrors a real production constraint: the same float problem crashed
GenVM on chain (tx `0xbdfb3673…d710`), which is why ForecastLedger's prompt
requires the price as a **string**. The mock in `tests/direct` therefore
replies with `json.dumps({"price": str(price)})`; patch 3 is defence in
depth for any mock that slips a float through.
