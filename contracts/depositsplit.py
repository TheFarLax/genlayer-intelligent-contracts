# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import datetime
from dataclasses import dataclass

from genlayer import *
from genlayer.gl import get_contract_at

# --- Split states -----------------------------------------------------------
OPEN = "open"
FINALIZED = "finalized"
VOID = "void"

MAX_TITLE_CHARS = 120
MAX_RECIPIENTS = 12

_ZERO = "0x0000000000000000000000000000000000000000"


@allow_storage
@dataclass
class Split:
    split_id: str
    creator: Address
    title: str
    total: u256
    released: u256
    recipient_count: u256
    status: str
    created_at: u256
    closed_at: u256


@allow_storage
@dataclass
class Share:
    recipient: Address
    weight: u256
    paid: bool
    paid_amount: u256


class DepositSplit(gl.Contract):
    splits: TreeMap[str, Split]
    # (split_id, index) -> Share. Flat keys avoid nested containers, which are
    # awkward to build incrementally in storage.
    shares: TreeMap[str, Share]
    # creator -> DynArray[split_id]
    by_creator: TreeMap[Address, DynArray[str]]
    # split_id -> amount already withdrawn per recipient, keyed for idempotency
    withdrawn: TreeMap[str, u256]
    seq: u256

    def __init__(self):
        self.seq = u256(0)

    # -- creation -----------------------------------------------------------

    @gl.public.write.payable
    def create(self, title: str, recipients: list, weights: list) -> str:
        """Create a funded split.

        `recipients` are addresses, `weights` are integer shares. The native
        value attached to this call is the pot. Weights are normalised to the
        pot at payout time, so the caller does not need to compute amounts in
        advance and rounding is handled once, in one place.

        Value is accepted only here. Every later call is non-payable, so a
        stray transfer cannot silently inflate the pot after the split has
        been announced to its recipients.
        """
        title = (title or "").strip()
        if title == "":
            raise gl.vm.UserError("title is required")
        if len(title) > MAX_TITLE_CHARS:
            raise gl.vm.UserError("title too long")

        value = int(gl.message.value)
        if value <= 0:
            raise gl.vm.UserError("deposit must be positive")

        if not isinstance(recipients, list) or not isinstance(weights, list):
            raise gl.vm.UserError("recipients and weights must be lists")
        if len(recipients) != len(weights):
            raise gl.vm.UserError("recipients and weights must be the same length")
        if len(recipients) == 0:
            raise gl.vm.UserError("at least one recipient is required")
        if len(recipients) > MAX_RECIPIENTS:
            raise gl.vm.UserError("too many recipients")

        addr_list = []
        weight_list = []
        total_weight = 0
        seen = set()
        for i in range(len(recipients)):
            raw_addr = str(recipients[i]).strip()
            try:
                recipient = Address(raw_addr)
            except Exception:
                raise gl.vm.UserError(f"invalid recipient address at index {i}")
            if recipient == Address(_ZERO):
                raise gl.vm.UserError(f"zero address at index {i}")
            if addr_list and any(recipient == a for a in addr_list):
                # A duplicate address would need two separate `paid` flags and
                # invite double payment; require the caller to merge weights.
                raise gl.vm.UserError(f"duplicate recipient at index {i}")

            try:
                weight = int(weights[i])
            except (TypeError, ValueError):
                raise gl.vm.UserError(f"invalid weight at index {i}")
            if weight <= 0:
                raise gl.vm.UserError(f"weight must be positive at index {i}")

            addr_list.append(recipient)
            weight_list.append(weight)
            total_weight += weight
            seen.add(recipient)

        n = int(self.seq)
        split_id = f"sp-{n}"
        sender = gl.message.sender_address
        if not isinstance(sender, Address):
            # Some hosts surface the sender as a hex string; storage requires
            # the Address type for an Address field.
            sender = Address(str(sender))

        self.splits[split_id] = Split(
            split_id=split_id,
            creator=sender,
            title=title,
            total=u256(value),
            released=u256(0),
            recipient_count=u256(len(addr_list)),
            status=OPEN,
            created_at=u256(_now()),
            closed_at=u256(0),
        )
        for i in range(len(addr_list)):
            self.shares[f"{split_id}:{i}"] = Share(
                recipient=addr_list[i],
                weight=u256(weight_list[i]),
                paid=False,
                paid_amount=u256(0),
            )
        self.by_creator.get_or_insert_default(sender).append(split_id)
        self.seq = u256(n + 1)
        return split_id

    # -- payout -------------------------------------------------------------

    @gl.public.write
    def claim(self, split_id: str) -> int:
        """Pay the caller their share of an open split.

        Deterministic only: the amount is a pure function of stored weights and
        the deposited total, so there is no nondeterministic block anywhere in
        this path and nothing that can end UNDETERMINED.

        Duplicate settlement is prevented by the per-share `paid` flag, which
        is written in the same transaction as the transfer.
        """
        s = self.splits.get(split_id)
        if s is None:
            raise gl.vm.UserError("split not found")
        if s.status != OPEN:
            raise gl.vm.UserError("split is not open")

        caller = gl.message.sender_address
        if not isinstance(caller, Address):
            caller = Address(str(caller))
        index = _find_share_index(self, split_id, int(s.recipient_count), caller)
        if index < 0:
            raise gl.vm.UserError("caller is not a recipient")

        key = f"{split_id}:{index}"
        share = self.shares.get(key)
        if share is None:
            raise gl.vm.UserError("share missing")
        if share.paid:
            raise gl.vm.UserError("already paid")

        amount = _share_amount(int(s.total), int(share.weight),
                               _total_weight(self, split_id, int(s.recipient_count)))

        # Mark paid BEFORE the transfer and in the same call. If the transfer
        # fails, the whole transaction reverts and `paid` rolls back with it,
        # so the flag can never be set without the value moving.
        share.paid = True
        share.paid_amount = u256(amount)
        self.shares[key] = share

        s.released = u256(int(s.released) + amount)
        if int(s.released) >= int(s.total):
            s.status = FINALIZED
            s.closed_at = u256(_now())
        self.splits[split_id] = s

        # Emitted after the storage writes, outside any nondet block.
        get_contract_at(caller).emit_transfer(value=u256(amount))
        return amount

    # -- recovery -----------------------------------------------------------

    @gl.public.write
    def void(self, split_id: str) -> str:
        """Cancel an open split and return the unclaimed remainder to its creator.

        This is the explicit recovery path a creator needs when recipients
        cannot or will not claim. All shares must still be unpaid: once anyone
        has been paid, voiding would leave partial settlement that cannot be
        reversed, so the split can only finish by claims.
        """
        s = self.splits.get(split_id)
        if s is None:
            raise gl.vm.UserError("split not found")
        if s.status != OPEN:
            raise gl.vm.UserError("split is not open")
        caller = gl.message.sender_address
        if not isinstance(caller, Address):
            caller = Address(str(caller))
        if caller != s.creator:
            raise gl.vm.UserError("only the creator may void")

        count = int(s.recipient_count)
        for i in range(count):
            share = self.shares.get(f"{split_id}:{i}")
            if share is not None and share.paid:
                raise gl.vm.UserError("cannot void after a payout")

        refund = int(s.total)
        s.status = VOID
        s.closed_at = u256(_now())
        s.released = u256(refund)
        self.splits[split_id] = s

        get_contract_at(s.creator).emit_transfer(value=u256(refund))
        return VOID

    # -- views --------------------------------------------------------------

    @gl.public.view
    def get_split(self, split_id: str) -> dict:
        s = self.splits.get(split_id)
        if s is None:
            return {"found": False, "split_id": split_id}
        return {
            "found": True,
            "split_id": s.split_id,
            "creator": s.creator.as_hex,
            "title": s.title,
            "total": int(s.total),
            "released": int(s.released),
            "recipient_count": int(s.recipient_count),
            "status": s.status,
            "created_at": int(s.created_at),
            "closed_at": int(s.closed_at),
        }

    @gl.public.view
    def get_share(self, split_id: str, index: int) -> dict:
        share = self.shares.get(f"{split_id}:{index}")
        if share is None:
            return {"found": False}
        return {
            "found": True,
            "recipient": share.recipient.as_hex,
            "weight": int(share.weight),
            "paid": bool(share.paid),
            "paid_amount": int(share.paid_amount),
        }

    @gl.public.view
    def preview(self, split_id: str, index: int) -> int:
        """Exact payout for one share, computed the same way `claim` does."""
        s = self.splits.get(split_id)
        share = self.shares.get(f"{split_id}:{index}")
        if s is None or share is None:
            return 0
        return _share_amount(
            int(s.total), int(share.weight),
            _total_weight(self, split_id, int(s.recipient_count)),
        )

    @gl.public.view
    def count(self) -> u256:
        return self.seq


# --- helpers ----------------------------------------------------------------

def _now() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def _find_share_index(contract, split_id: str, count: int, addr) -> int:
    for i in range(count):
        share = contract.shares.get(f"{split_id}:{i}")
        if share is not None and share.recipient == addr:
            return i
    return -1


def _total_weight(contract, split_id: str, count: int) -> int:
    total = 0
    for i in range(count):
        share = contract.shares.get(f"{split_id}:{i}")
        if share is not None:
            total += int(share.weight)
    return total


def _share_amount(total: int, weight: int, total_weight: int) -> int:
    """Floor payout for one weight.

    Flooring means the sum of payouts can be less than the pot by at most
    `recipients - 1` wei of dust per split. That dust stays in the contract
    rather than being pushed to the last claimant, because pushing it makes the
    claimant's payout depend on claim order.
    """
    if total_weight <= 0:
        return 0
    return (total * weight) // total_weight
