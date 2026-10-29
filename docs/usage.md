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

## Reproducing the test environment

The 71 direct-mode tests need **three patches to the installed `gltest`
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
/root/glvenv/bin/python -m pytest tests/direct -q     # 71 passed
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
