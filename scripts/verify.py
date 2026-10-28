"""End-to-end verification for the bundle-1 contracts on Studionet.

Every check here talks to the live chain through `glnet`; nothing is simulated.
Each function prints a line per assertion so the output can be pasted into the
README as evidence, and the script exits non-zero on the first failure.

Usage:
    /root/glvenv/bin/python scripts/verify.py <contract> [address]

`address` may be omitted if `deployments/<contract>.json` exists.
"""

from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import glnet


def _load(contract: str, address: str | None) -> str:
    if address:
        return address
    path = glnet.DEPLOYMENTS / f"{contract}.json"
    if not path.exists():
        raise SystemExit(f"no deployment record for {contract}; deploy it first")
    return json.loads(path.read_text())["address"]


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        raise SystemExit(1)


def _expect_revert(label: str, fn, needle: str, address: str = "") -> None:
    """A rejection is only real evidence if the tx result carries the revert.

    On Studionet a `UserError` revert is reported as a FINALIZED transaction
    whose `result` field is the revert message (exit code 1), not as an RPC
    exception, so the message must be read from the transaction record.
    """
    tx, info = fn()
    if not address:
        # The receipt tells us which contract the tx targeted.
        address = _tx_target(info)
    client = glnet.chain_client()
    text = ""
    try:
        raw = client.provider.make_request(
            method="sim_getTransactionsForAddress", params=[address]
        ).get("result") or []
        for t in raw:
            if t.get("hash") == tx:
                text = str(t.get("result") or "")
                break
    except Exception:  # noqa: BLE001 - the check below reports the failure
        pass
    _check(label, needle.lower() in text.lower(), text[:160] or "(no result)")


def _tx_target(info: dict) -> str:
    receipt = info.get("receipt") or {}
    to = receipt.get("toAddress") or receipt.get("to") or ""
    return to if isinstance(to, str) else ""


def _retry(fn, n: int = 30, delay: float = 4.0):
    """Tolerate transient DNS/RPC failures without hiding real errors."""
    last = None
    for _ in range(n):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            if "resolve" not in msg and "Connection" not in msg and "timed out" not in msg:
                raise
            time.sleep(delay)
    raise last  # type: ignore[misc]


# --- ClaimPin ---------------------------------------------------------------

CLAIM_URL = "https://example.com/status"


def verify_claimpin(address: str) -> None:
    print(f"ClaimPin @ {address}")

    before = _retry(lambda: glnet.read(address, "count"))
    tx, info = _retry(lambda: glnet.write(address, "register", [
        "Service is operational",
        CLAIM_URL,
        "The page must state that all systems are operational.",
        60,
    ]))
    print(f"   register tx {tx} -> {info['status']}")

    cid = f"claim-{before}"
    rec = _retry(lambda: glnet.read(address, "get_claim", [cid]))
    _check("claim is readable", rec.get("found") is True, str(rec)[:160])

    _expect_revert("empty claim rejected", lambda: glnet.write(address, "register", ["", CLAIM_URL, "criteria", 60])                   , "claim is required", address)
    _expect_revert("non-http source rejected", lambda: glnet.write(address, "register", ["c", "ftp://x", "criteria", 60])                   , "http", address)
    _check("count unchanged after rejections",
           _retry(lambda: glnet.read(address, "count")) == before + 1)


# --- OutcomeFeed ------------------------------------------------------------


def verify_outcomefeed(address: str) -> None:
    """The deployer wallet IS the producer, so an append from this session
    exercises the producer path. The non-producer rejection is covered by the
    direct tests; here we verify the append semantics live: stored entry,
    correct fields, and idempotency on a duplicate ref."""
    print(f"OutcomeFeed @ {address}")

    producer = _retry(lambda: glnet.read(address, "get_producer"))
    print(f"   producer {producer}")
    _check("producer is set", producer.startswith("0x") and int(producer, 16) != 0)

    before = _retry(lambda: glnet.read(address, "count"))
    ref = f"ref-live-{before}"
    tx, info = _retry(lambda: glnet.write(address, "append",
                                         [ref, "eth", "hit", 3_100_000_000, "deadbeef", "above"]))
    print(f"   append tx {tx} -> {info['status']}")
    key = _retry(lambda: glnet.read(address, "key_for", [ref]))
    _check("entry indexed by ref", key != "", key)
    entry = _retry(lambda: glnet.read(address, "get", [key]))
    _check("entry readable", entry.get("found") is True, str(entry)[:160])
    _check("subject upper-cased", entry.get("subject") == "ETH")
    _check("status recorded", entry.get("status") == "hit")
    _check("observed micros recorded", entry.get("observed_micros") == 3_100_000_000)
    _check("count advanced",
           _retry(lambda: glnet.read(address, "count")) == before + 1)

    # Idempotency: re-send the same ref; it must converge, not append.
    tx2, _ = _retry(lambda: glnet.write(address, "append",
                                        [ref, "eth", "miss", 1, "x", "retry"]))
    key2 = _retry(lambda: glnet.read(address, "key_for", [ref]))
    _check("duplicate ref converges to the same key", key2 == key, f"{key2}")
    _check("count unchanged after duplicate",
           _retry(lambda: glnet.read(address, "count")) == before + 1)
    _check("first outcome wins on retry",
           _retry(lambda: glnet.read(address, "get", [key])).get("status") == "hit")

    tally = _retry(lambda: glnet.read(address, "subject_tally", ["ETH"]))
    _check("tally counts the live entry",
           tally.get("hits", 0) >= 1, str(tally)[:120])

    _expect_revert("non-decided status rejected",
                   lambda: glnet.write(address, "append",
                                       [f"bad-{before}", "ETH", "pending", 1, "", ""]),
                   "status must be hit or miss", address)
    _check("count unchanged after rejection",
           _retry(lambda: glnet.read(address, "count")) == before + 1)


# --- ForecastLedger ---------------------------------------------------------

PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"


def verify_forecastledger(address: str) -> None:
    print(f"ForecastLedger @ {address}")

    before = _retry(lambda: glnet.read(address, "count"))
    deadline = int(time.time()) + 86400
    tx, info = _retry(lambda: glnet.write(address, "register", [
        "ETH", "up", 1_000_000, deadline, PRICE_URL, "smoke",
    ]))
    print(f"   register tx {tx} -> {info['status']}")

    fid = f"fc-{before}"
    rec = _retry(lambda: glnet.read(address, "get_forecast", [fid]))
    _check("forecast is readable", rec.get("found") is True, str(rec)[:160])
    _check("status pending", rec.get("status") == "pending")

    _expect_revert("past deadline rejected", lambda: glnet.write(address, "register",
                                       ["ETH", "up", 1, 1000000000, PRICE_URL, ""])                   , "deadline must be in the future", address)
    _expect_revert("bad direction rejected", lambda: glnet.write(address, "register",
                                       ["ETH", "sideways", 1, deadline, PRICE_URL, ""])                   , "direction must be", address)
    _expect_revert("settling before the deadline rejected", lambda: glnet.write(address, "settle", [fid])                   , "not yet due", address)
    _check("count unchanged after rejections",
           _retry(lambda: glnet.read(address, "count")) == before + 1)


# --- DepositSplit -----------------------------------------------------------

RECIPIENTS = ["0x" + "b1" * 20, "0x" + "c2" * 20]


def verify_depositsplit(address: str) -> None:
    print(f"DepositSplit @ {address}")

    before = _retry(lambda: glnet.read(address, "count"))
    tx, info = _retry(lambda: glnet.write(
        address, "create", ["verification split", RECIPIENTS, [1, 3]], value=1000
    ))
    print(f"   create tx {tx} -> {info['status']}")

    sid = f"sp-{before}"
    s = _retry(lambda: glnet.read(address, "get_split", [sid]))
    _check("split is readable", s.get("found") is True, str(s)[:200])
    _check("deposit recorded", s.get("total") == 1000, f"total={s.get('total')}")
    _check("share 0 previews 250", _retry(
        lambda: glnet.read(address, "preview", [sid, 0])) == 250)
    _check("share 1 previews 750", _retry(
        lambda: glnet.read(address, "preview", [sid, 1])) == 750)

    _expect_revert("zero deposit rejected", lambda: glnet.write(address, "create", ["t", RECIPIENTS, [1, 1]])                   , "deposit must be positive", address)
    _expect_revert("duplicate recipient rejected", lambda: glnet.write(address, "create",
                                       ["t", [RECIPIENTS[0], RECIPIENTS[0]], [1, 1]],
                                       value=10)                   , "duplicate recipient", address)
    _check("count unchanged after rejections",
           _retry(lambda: glnet.read(address, "count")) == before + 1)


VERIFIERS = {
    "claimpin": verify_claimpin,
    "outcomefeed": verify_outcomefeed,
    "forecastledger": verify_forecastledger,
    "depositsplit": verify_depositsplit,
}


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in VERIFIERS:
        raise SystemExit(f"usage: verify.py {{{','.join(VERIFIERS)}}} [address]")
    name = sys.argv[1]
    VERIFIERS[name](_load(name, sys.argv[2] if len(sys.argv) > 2 else None))
    print("  ALL CHECKS PASSED")
