"""Shared GenLayer testnet helpers for the bundle-1 contracts.

Network policy (enforced, not advisory):
  * Studionet only. Any other chain id is refused before a transaction is built.
  * The signing account is the dedicated keystore wallet ``bundle1-builder``.
    Its private key lives in /root/.genlayer/keystores/ (created by
    ``genlayer account create``) and is never read, printed, or logged here --
    only the public address is ever surfaced.

Why this module exists instead of raw shell calls: deployments need a receipt
wait plus address extraction plus optional code verification, and the CLI's
output formatting is not a stable interface to parse.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from genlayer_py import create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

STUDIONET_ID = 61999
STUDIONET_RPC = "https://studio.genlayer.com/api"
ACCOUNT_NAME = "bundle1-builder"

REPO = Path(__file__).resolve().parent.parent
CONTRACTS = REPO / "contracts"
DEPLOYMENTS = REPO / "deployments"


def _fail(msg: str) -> None:
    print(f"REFUSED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def guard_network(client) -> None:
    """Refuse to act unless the live endpoint is Studionet."""
    if studionet.id != STUDIONET_ID:
        _fail(f"SDK chain id {studionet.id} != {STUDIONET_ID}")
    live = int(client.provider.make_request(method="eth_chainId", params=[])["result"], 16)
    if live != STUDIONET_ID:
        _fail(f"endpoint chain id {live} != {STUDIONET_ID} (not Studionet)")


def account_address(name: str = ACCOUNT_NAME) -> str:
    """Public address only. The keystore password never appears here."""
    out = subprocess.run(
        ["genlayer", "account", "list"], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        stripped = line.replace("*", " ").split()
        if len(stripped) >= 2 and stripped[0] == name:
            return stripped[1]
    _fail(f"keystore account {name!r} not found; create it with `genlayer account create --name {name}`")
    raise AssertionError  # unreachable


def chain_client():
    """A read-only client bound to Studionet, guarded against misconfiguration."""
    client = create_client(chain=studionet)
    guard_network(client)
    return client


def balance_wei(addr: str) -> int:
    client = chain_client()
    return int(client.provider.make_request(method="eth_getBalance", params=[addr, "latest"])["result"], 16)


def _find_contract_address(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for key in ("contract_address", "to"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith("0x") and len(v) == 42 and int(v, 16) != 0:
                return v
        for v in obj.values():
            found = _find_contract_address(v)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _find_contract_address(v)
            if found:
                return found
    return None


def wait(transaction_hash: str, tries: int = 120) -> Any:
    client = chain_client()
    last = None
    for _ in range(tries):
        try:
            receipt = client.wait_for_transaction_receipt(
                transaction_hash=transaction_hash,
                status=TransactionStatus.FINALIZED,
                retries=1,
                interval=2000,
            )
            return receipt
        except Exception as e:  # noqa: BLE001 - polling, any error is retryable
            last = e
            status = client.provider.make_request(
                method="gen_getTransactionStatus", params=[transaction_hash]
            ).get("result")
            if status in ("UNDETERMINED", "CANCELED"):
                raise RuntimeError(f"transaction ended {status}: {last}") from last
            time.sleep(2)
    raise RuntimeError(f"timed out waiting for {transaction_hash}: {last}")


def receipt_status(transaction_hash: str) -> dict:
    """Consensus status plus the raw receipt, for recording real evidence."""
    client = chain_client()
    status = client.provider.make_request(
        method="gen_getTransactionStatus", params=[transaction_hash]
    )
    try:
        receipt = client.provider.make_request(
            method="eth_getTransactionReceipt", params=[transaction_hash]
        )
    except Exception as e:  # noqa: BLE001
        receipt = {"error": str(e)}
    return {"status": status.get("result"), "receipt": receipt.get("result")}


def deploy(contract: str, args: Optional[list] = None, account_name: str = ACCOUNT_NAME) -> dict:
    """Deploy contracts/<contract>.py through consensus. Returns recorded evidence."""
    from genlayer_py import create_account  # local import keeps key handling tight

    src = CONTRACTS / f"{contract}.py"
    if not src.exists():
        _fail(f"no such contract source: {src}")
    code = src.read_text(encoding="utf-8")

    client = chain_client()
    signer = _load_signer(account_name)
    addr = signer.address

    bal = balance_wei(addr)
    if bal <= 0:
        _fail(f"{addr} has no GEN on Studionet; fund it before deploying")

    tx = client.deploy_contract(code=code, args=args or [], account=signer)
    print(f"[deploy] {contract}: tx {tx}")
    print(f"[deploy] waiting for FINALIZED ...")
    wait(tx)
    info = receipt_status(tx)
    address = _find_contract_address(info)
    if not address:
        raise RuntimeError(f"no contract address in receipt: {json.dumps(info)[:1500]}")

    record = {
        "contract": contract,
        "network": "studionet",
        "chain_id": STUDIONET_ID,
        "rpc": STUDIONET_RPC,
        "address": address,
        "deployer": addr,
        "deploy_tx": tx,
        "status": info["status"],
        "constructor_args": args or [],
        "source_sha256": _sha256(code),
    }
    DEPLOYMENTS.mkdir(exist_ok=True)
    (DEPLOYMENTS / f"{contract}.json").write_text(json.dumps(record, indent=2))
    print(f"[deploy] {contract} @ {address} ({info['status']})")
    return record


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PW_FILE = Path.home() / ".genlayer" / f".{ACCOUNT_NAME}.pw"


def _load_signer(name: str):
    """Build a LocalAccount from the encrypted CLI keystore.

    The keystore is decrypted in memory only. The plaintext key is never
    returned to callers, printed, or written anywhere.
    """
    from genlayer_py import create_account

    if name != ACCOUNT_NAME:
        _fail(f"refusing to sign with non-dedicated account {name!r}")
    keystore = Path.home() / ".genlayer" / "keystores" / f"{name}.json"
    if not keystore.exists():
        _fail(f"keystore missing: {keystore}")
    if not _PW_FILE.exists():
        _fail(f"keystore password file missing: {_PW_FILE}")
    password = _PW_FILE.read_text().strip()

    try:
        from eth_account import Account
    except ImportError:
        _fail("eth_account not available")

    with open(keystore) as f:
        enc = json.load(f)
    key = Account.decrypt(enc, password)
    return create_account(key.hex() if hasattr(key, "hex") else key)


def read(address: str, function: str, args: Optional[list] = None, account_name: str = ACCOUNT_NAME):
    """Read-only view call. `gen_call` needs a `from` address but signs nothing.

    Studionet's node occasionally answers a view simulround-trip with a
    transient `execution failed`. A view has no side effects, so a couple of
    retries is safe; a genuine contract error reproduces on every attempt and
    is raised after the last one.
    """
    from genlayer_py import create_client

    signer = _load_signer(account_name)
    client = create_client(chain=studionet, account=signer)
    guard_network(client)

    last = None
    for attempt in range(3):
        try:
            return client.read_contract(
                address=address, function_name=function, args=args or []
            )
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            last = e
            if "execution failed" not in str(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def write(address: str, function: str, args: Optional[list] = None, value: int = 0, account_name: str = ACCOUNT_NAME):
    signer = _load_signer(account_name)
    tx = chain_client().write_contract(
        address=address, function_name=function, args=args or [], value=value, account=signer
    )
    print(f"[write] {function}{tuple(args or [])} -> tx {tx}")
    wait(tx)
    info = receipt_status(tx)
    print(f"[write] {function}: {info['status']}")
    return tx, info


if __name__ == "__main__":
    print("account:", account_address())
    print("balance_wei:", balance_wei(account_address()))
    os._exit(0)
