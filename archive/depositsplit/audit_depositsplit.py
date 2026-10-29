"""Live evidence for DepositSplit's rounding fix and its payout limitation.

This is a *read-only* audit of what happened on Studionet on 2026-10-28, kept
so the claims in README.md / docs/usage.md can be re-checked without spending
GEN. It reads transactions and balances; it sends nothing.

    /root/glvenv/bin/python scripts/audit_depositsplit.py

Why read-only: the payout leg is broken for wallet recipients (see the
"CRITICAL" note in docs/usage.md), so there is nothing left to prove by
sending more transactions. What matters is the recorded evidence.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import glnet

CONTRACT = "0x1D2202D5a55f2DA6Fd51BA32503866E60E83d315"
CREATOR = "0x05C55d0d2Ba7fe495492849fEca1BF2bBF939a4C"

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def tx_index(address):
    client = glnet.chain_client()
    raw = client.provider.make_request(
        method="sim_getTransactionsForAddress", params=[address]
    ).get("result") or []
    return {str(t.get("hash")): t for t in raw if t.get("hash")}


def main():
    rec = json.loads((glnet.DEPLOYMENTS / "depositsplit.json").read_text())
    check("deployed address matches the record", rec["address"] == CONTRACT, CONTRACT)
    check("deployment is on chain 61999", rec["chain_id"] == 61999)

    print("\n[rounding fix] sp-0: 1000 across three equal weights")
    s0 = glnet.read(CONTRACT, "get_split", ["sp-0"])
    check("sp-0.total is 1000", s0["total"] == 1000, str(s0["total"]))
    check("sp-0 finalized", s0["status"] == "finalized", s0["status"])
    check("sp-0 released the whole pot", s0["released"] == 1000, str(s0["released"]))
    shares0 = [glnet.read(CONTRACT, "get_share", ["sp-0", i]) for i in range(3)]
    amounts = [s["paid_amount"] for s in shares0]
    check("shares are 334/333/333", amounts == [334, 333, 333], str(amounts))
    check("shares sum to the pot", sum(amounts) == 1000, str(sum(amounts)))
    check("all shares marked paid", all(s["paid"] for s in shares0))
    # The old floor logic would have produced 333/333/333 = 999.
    check("not the old flooring result", amounts != [333, 333, 333], str(amounts))

    print("\n[recovery] sp-1: one claim then void, remainder refunded")
    s1 = glnet.read(CONTRACT, "get_split", ["sp-1"])
    check("sp-1 voided", s1["status"] == "void", s1["status"])
    sh = [glnet.read(CONTRACT, "get_share", ["sp-1", i]) for i in range(2)]
    check("first share paid 500", sh[0]["paid"] and sh[0]["paid_amount"] == 500)
    check("second share untouched", not sh[1]["paid"])
    check("remainder was 500", s1["total"] - sh[0]["paid_amount"] == 500)

    print("\n[payout limitation] transfers that did NOT reach a wallet")
    idx = tx_index(CONTRACT)
    expected_failures = {
        "0x4a76cbcd7483e724458cfa9d3105bf065b7f4da8de3b45b471d9683a430e1d77": 334,
        "0xace64d91e72e41": 333,
        "0x98b85dff72bef5": 333,
        "0xc046352794b5785a64d9119669ac123a373c885ace03b2cc0c8a7e51e165fd2b": 500,
        "0x97f80f95ab2d86": 500,
    }
    checked = 0
    for prefix, value in expected_failures.items():
        matches = [t for h, t in idx.items() if h.startswith(prefix)]
        if not matches:
            check(f"tx {prefix} present", False)
            continue
        t = matches[0]
        checked += 1
        check(f"tx {prefix} carried {value}",
              t.get("value") == value, str(t.get("value")))
        check(f"tx {prefix} was NOT credited",
              t.get("value_credited") is False, str(t.get("value_credited")))
        check(f"tx {prefix} failed with 'not found'",
              "not found" in str(t.get("result")), str(t.get("result"))[:48])
    check("all expected failing transfers found", checked == len(expected_failures),
          f"{checked}/{len(expected_failures)}")

    print("\n[consequence] the funds stayed in the contract")
    bal = glnet._retry_net(lambda: glnet.balance_wei(CONTRACT))
    check("contract still holds the un-delivered value", bal > 0, f"{bal} wei")

    print("\n" + ("AUDIT PASSED" if not FAILS else f"FAILURES: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
