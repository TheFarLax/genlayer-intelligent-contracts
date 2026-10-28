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

        weights = _weights(self, split_id, int(s.recipient_count))
        amounts = _apportion(int(s.total), weights)
        amount = amounts[index]

        # Mark paid BEFORE the transfer and in the same call. If the transfer
        # fails, the whole transaction reverts and `paid` rolls back with it,
        # so the flag can never be set without the value moving.
        share.paid = True
        share.paid_amount = u256(amount)
        self.shares[key] = share

        s.released = u256(int(s.released) + amount)
        # Close when no share is left unpaid. Checking the shares themselves is
        # stricter than comparing `released` to `total`: it holds even if some
        # share is worth zero units, and it cannot be fooled by accounting
        # drift. The two agree once apportionment sums to `total`.
        if _unpaid_count(self, split_id, int(s.recipient_count)) == 0:
            s.status = FINALIZED
            s.closed_at = u256(_now())
        self.splits[split_id] = s

        # Emitted after the storage writes, outside any nondet block.
        # Apportionment can assign a share 0 units when the pot has fewer
        # units than recipients. `emit_transfer` rejects a zero value, so
        # skip it: the share is still marked paid, which lets the split close
        # instead of crashing on the last claimant and stranding the split.
        if amount > 0:
            get_contract_at(caller).emit_transfer(value=u256(amount))
        return amount

    # -- recovery -----------------------------------------------------------

    @gl.public.write
    def void(self, split_id: str) -> str:
        """Cancel an open split and refund the UNCLAIMED remainder.

        This is the explicit recovery path a creator needs when recipients
        cannot or will not claim. It refunds exactly what is still held:
        `total - released`. Before, void was refused as soon as any single
        share had been paid, which meant a split whose remaining recipients
        were unreachable kept their funds forever. Refunding the remainder is
        safe because no paid share can be revived — the split moves to VOID,
        which `claim` rejects.
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

        released = int(s.released)
        refund = int(s.total) - released
        if refund < 0:
            # Defensive: released should never exceed total.
            raise gl.vm.UserError("split accounting is inconsistent")

        s.status = VOID
        s.closed_at = u256(_now())
        s.released = u256(int(s.total))
        self.splits[split_id] = s

        # Nothing to send when every unit was already claimed; emitting a
        # zero-value transfer would be a wasted cross-contract message.
        if refund > 0:
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
        return _apportion(
            int(s.total), _weights(self, split_id, int(s.recipient_count))
        )[index]

    @gl.public.view
    def preview_all(self, split_id: str) -> list:
        """Every share's payout, in index order. Sums to `total`."""
        s = self.splits.get(split_id)
        if s is None:
            return []
        return _apportion(
            int(s.total), _weights(self, split_id, int(s.recipient_count))
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


def _unpaid_count(contract, split_id: str, count: int) -> int:
    n = 0
    for i in range(count):
        share = contract.shares.get(f"{split_id}:{i}")
        if share is not None and not share.paid:
            n += 1
    return n


def _share_amount(total: int, weight: int, total_weight: int) -> int:
    """Floor payout for one weight.

    Retained for callers that only have one share's data. The contract itself
    uses `_apportion` so that a pot is fully distributed; see that function.
    """
    if total_weight <= 0:
        return 0
    return (total * weight) // total_weight


def _weights(contract, split_id: str, count: int) -> list:
    out = []
    for i in range(count):
        share = contract.shares.get(f"{split_id}:{i}")
        out.append(int(share.weight) if share is not None else 0)
    return out


def _apportion(total: int, weights: list) -> list:
    """Split `total` so the parts sum to exactly `total`.

    Plain flooring (`total * weight // total_weight`) leaves up to
    `recipients - 1` units stranded. Those units were unrecoverable: the split
    never reached `finalized`, because `released` could not equal `total`, and
    `void` refuses once any share is paid. A 1000 pot across three equal
    weights paid 333 + 333 + 333 and left 1 unit locked forever.

    This is largest-remainder (Hamilton) apportionment:

      1. give every share its floor,
      2. distribute the leftover units one each, to the shares with the
         largest remainders,
      3. break remainder ties by lower index.

    Every share lands within [floor, ceil] of its exact quota, so nobody is
    under- or over-paid by more than one unit. The result depends only on the
    stored weights and `total` — never on claim order — because the tie-break
    is a fixed function of the index. It is recomputed identically at every
    `claim`, so it needs no extra storage and no migration.
    """
    tw = sum(weights)
    if tw <= 0:
        return [0] * len(weights)
    base = [(total * w) // tw for w in weights]
    rem = [(total * w) % tw for w in weights]
    left = total - sum(base)
    # Only `left < len(weights)` can occur, but guard anyway in case of a
    # future change to how weights are stored.
    if left > 0:
        order = sorted(range(len(weights)), key=lambda i: (-rem[i], i))
        for i in order[:left]:
            base[i] += 1
    return base

