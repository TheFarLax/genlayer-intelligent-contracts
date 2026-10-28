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

## DepositSplit — `0x9aD2b1fDF07E64B64538B8E38F417B6FF9247C87`

Splits a deposited GEN pot across weighted recipients; each recipient claims
their own share once.

> **State of the live contract (checked 2026-10-28):** `count = 2`,
> `balance = 2020` wei. Two open splits of 1000 each are stored (`sp-0`,
> `sp-1`, the latter from a verification run with weights [1,3] previewing
> 250/750). The 20 wei on top of the 2000 deposits matches two 10-wei
> duplicate-recipient `create` calls that reverted: the revert discarded the
> *state* change (count did not advance) while the attached value appears to
> have stayed with the contract. That is inferred from the balance, not from a
> transfer trace. **No `claim` or `void` has ever been sent to this address**,
> which is why those paths remain unverified on chain.

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

Payouts floor: `total * weight // total_weight`. The `paid` flag is written
before the transfer in the same transaction, so a failed transfer rolls the
flag back with it. The split moves to `status: "finalized"` when `released`
reaches `total`.

**Caveat on floors:** the remainder is only fully distributed when the floor
division happens to be exact. With `total=1000`, `weights=[1,3]` the shares
are 250 and 750 and sum exactly. With `total=1000`, `weights=[1,1,1]` each
share floors to 333, `released` reaches 999 < 1000, and the split **never
becomes `finalized`** — the last wei stays in the contract and `void` is
blocked because a share has been paid. USD-style amounts in micros make this
negligible in practice, but it is real and untested on chain.

**This path is covered by direct-mode tests only — see the verification
table below.** Reverts: `split not found`, `split is not open`,
`caller is not a recipient`, `already paid`.

### `void(split_id) -> "void"`

```python
glnet.write(DEPOSIT, "void", ["sp-0"])   # as the creator
```

Creator-only, only while every share is still unpaid. Marks the split
`void`, sets `released = total`, and emits a full refund of `total` back to
the creator. Reverts: `split not found`, `split is not open`,
`only the creator may void`, `cannot void after a payout`. Also covered by
direct tests only.

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
| **DepositSplit `claim` actual GEN transfer** | yes | **NO — not exercised live** |
| **DepositSplit split closing after last claim** | yes | **NO — not exercised live** |
| **DepositSplit `void` refund transfer** | yes | **NO — not exercised live** |
| **DepositSplit `get_contract_at(...).emit_transfer` on chain** | mocked | **NO** |

### The DepositSplit caveat, stated plainly

Claim and void compute the right things and pass every direct test, but
**their value-moving behaviour has not been observed on Studionet.** Three
specific facts are unverified on chain:

1. That `emit_transfer(value=...)` to an externally-owned recipient actually
   delivers GEN and is finalizable to the recipient.
2. That the `paid`-before-transfer ordering behaves as designed when a
   transfer fails on chain. The design is sound (same-transaction rollback),
   but a failing transfer was never produced on Studionet to confirm it.
3. That `void`'s full refund to the creator lands.

Until those are run, treat DepositSplit as a working deterministic ledger of
who is owed what, whose payout leg is tested locally but unproven on chain.
Contract balance can be checked with `glnet.balance_wei(address)` against
`get_split(sid)["released"]` to see how far any live claim attempt got.

---

## Reproducing the test environment

The 95 direct-mode tests need **three patches to the installed `gltest`
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
/root/glvenv/bin/python -m pytest tests/direct -q     # 95 passed
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
