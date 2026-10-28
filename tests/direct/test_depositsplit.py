import pytest

CREATOR = "0x" + "aa" * 20
ALICE = "0x" + "b1" * 20
BOB = "0x" + "c2" * 20
CAROL = "0x" + "d3" * 20
OUTSIDER = "0x" + "ee" * 20


def _coin(base, n):
    return base + n


def _capture_transfers(direct_vm):
    """Record native transfers the contract emits."""
    seen = []

    def hook(vm, request):
        if isinstance(request, dict) and "PostMessage" in request:
            post = request["PostMessage"]
            seen.append(post)
            return {"ok": {}}
        return None

    direct_vm._gl_call_hook = hook
    return seen


def _as_prank(direct_vm, addr, value=0):
    direct_vm.sender = addr
    direct_vm.value = value


def _create(direct_vm, c, value, recipients, weights, title="team split"):
    _as_prank(direct_vm, CREATOR, value)
    try:
        return c.create(title, recipients, weights)
    finally:
        direct_vm.value = 0


# --- creation ---------------------------------------------------------------

def test_create_then_claim(direct_vm, direct_deploy):
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 1000, [ALICE, BOB], [1, 3])

    s = c.get_split(sid)
    assert s["total"] == 1000
    assert s["recipient_count"] == 2
    assert s["status"] == "open"
    assert s["released"] == 0

    # 1:3 split of 1000 -> 250 and 750, exactly.
    assert c.preview(sid, 0) == 250
    assert c.preview(sid, 1) == 750

    _as_prank(direct_vm, ALICE, 0)
    assert c.claim(sid) == 250
    assert len(transfers) == 1
    assert transfers[-1]["value"] == 250
    assert c.get_share(sid, 0)["paid"] is True
    assert c.get_share(sid, 0)["paid_amount"] == 250

    _as_prank(direct_vm, BOB, 0)
    assert c.claim(sid) == 750
    s = c.get_split(sid)
    assert s["status"] == "finalized"
    assert s["released"] == 1000
    assert s["closed_at"] > 0


def test_create_requires_value(direct_vm, direct_deploy):
    c = direct_deploy("contracts/depositsplit.py")
    with direct_vm.expect_revert("deposit must be positive"):
        _create(direct_vm, c, 0, [ALICE], [1])


@pytest.mark.parametrize("recipients,weights,needle", [
    ([ALICE], [1, 2], "same length"),
    ([], [], "at least one recipient"),
    ([ALICE, ALICE], [1, 2], "duplicate recipient"),
    ([ALICE], [0], "weight must be positive"),
    ([ALICE], [-1], "weight must be positive"),
    (["0x0000000000000000000000000000000000000000"], [1], "zero address"),
    (["not-an-address"], [1], "invalid recipient"),
    ([ALICE] * 13, [1] * 13, "too many recipients"),
])
def test_create_validation(direct_vm, direct_deploy, recipients, weights, needle):
    c = direct_deploy("contracts/depositsplit.py")
    with direct_vm.expect_revert(needle):
        _create(direct_vm, c, 100, recipients, weights)
    assert c.count() == 0


def test_create_requires_title(direct_vm, direct_deploy):
    c = direct_deploy("contracts/depositsplit.py")
    with direct_vm.expect_revert("title is required"):
        _create(direct_vm, c, 100, [ALICE], [1], title="   ")


# --- duplicate settlement ---------------------------------------------------

def test_double_claim_rejected(direct_vm, direct_deploy):
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE, BOB], [1, 1])

    _as_prank(direct_vm, ALICE)
    c.claim(sid)
    # The flag itself refuses the repeat, independent of whether the split
    # happens to have closed.
    with direct_vm.expect_revert("already paid"):
        c.claim(sid)
    assert len(transfers) == 1


def test_claim_on_closed_split_rejected(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, ALICE)
    c.claim(sid)
    assert c.get_split(sid)["status"] == "finalized"
    with direct_vm.expect_revert("not open"):
        c.claim(sid)


def test_non_recipient_cannot_claim(direct_vm, direct_deploy):
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, OUTSIDER)
    with direct_vm.expect_revert("not a recipient"):
        c.claim(sid)


def test_unknown_split_rejected(direct_vm, direct_deploy):
    c = direct_deploy("contracts/depositsplit.py")
    with direct_vm.expect_revert("split not found"):
        c.claim("sp-99")


# --- rounding ---------------------------------------------------------------

def test_floor_rounding_leaves_dust_and_never_overpays(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE, BOB, CAROL], [1, 1, 1])

    assert c.preview(sid, 0) == 33
    assert c.preview(sid, 1) == 33
    assert c.preview(sid, 2) == 33

    paid = 0
    for who in (ALICE, BOB, CAROL):
        _as_prank(direct_vm, who)
        paid += c.claim(sid)

    assert paid == 99
    s = c.get_split(sid)
    # One wei of dust stays in the contract; the pot is never exceeded.
    assert s["released"] == 99
    assert s["released"] <= s["total"]
    assert s["status"] == "open"   # dust means the pot was not fully released


def test_full_weight_to_single_recipient(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 777, [ALICE], [5])
    _as_prank(direct_vm, ALICE)
    assert c.claim(sid) == 777


def test_large_weights_do_not_overflow_proportion(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 10**12, [ALICE, BOB], [10**9, 3 * 10**9])
    _as_prank(direct_vm, ALICE)
    got = c.claim(sid)
    assert got == 10**12 // 4


# --- recovery ---------------------------------------------------------------

def test_creator_can_void_before_any_payout(direct_vm, direct_deploy):
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 500, [ALICE, BOB], [1, 1])

    _as_prank(direct_vm, CREATOR)
    assert c.void(sid) == "void"
    assert transfers[-1]["value"] == 500
    s = c.get_split(sid)
    assert s["status"] == "void"
    assert s["released"] == 500

    _as_prank(direct_vm, ALICE)
    with direct_vm.expect_revert("not open"):
        c.claim(sid)


def test_cannot_void_after_partial_payout(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE, BOB], [1, 1])

    _as_prank(direct_vm, ALICE)
    c.claim(sid)

    _as_prank(direct_vm, CREATOR)
    with direct_vm.expect_revert("after a payout"):
        c.void(sid)
    # The split is still open, and the second recipient can still be paid.
    _as_prank(direct_vm, BOB)
    assert c.claim(sid) == 50


def test_only_creator_can_void(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, ALICE)
    with direct_vm.expect_revert("only the creator may void"):
        c.void(sid)


def test_cannot_void_twice(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, CREATOR)
    c.void(sid)
    with direct_vm.expect_revert("not open"):
        c.void(sid)


# --- views ------------------------------------------------------------------

def test_missing_split_view(direct_vm, direct_deploy):
    c = direct_deploy("contracts/depositsplit.py")
    assert c.get_split("sp-0")["found"] is False
    assert c.get_share("sp-0", 0)["found"] is False
    assert c.preview("sp-0", 0) == 0


def test_ids_are_sequential_and_independent(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("contracts/depositsplit.py")
    a = _create(direct_vm, c, 10, [ALICE], [1])
    b = _create(direct_vm, c, 20, [BOB], [1])
    assert (a, b) == ("sp-0", "sp-1")
    assert c.get_split(a)["total"] == 10
    assert c.get_split(b)["total"] == 20
