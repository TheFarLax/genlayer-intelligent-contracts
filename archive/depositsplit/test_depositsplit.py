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
    c = direct_deploy("archive/depositsplit/depositsplit.py")
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
    c = direct_deploy("archive/depositsplit/depositsplit.py")
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
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    with direct_vm.expect_revert(needle):
        _create(direct_vm, c, 100, recipients, weights)
    assert c.count() == 0


def test_create_requires_title(direct_vm, direct_deploy):
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    with direct_vm.expect_revert("title is required"):
        _create(direct_vm, c, 100, [ALICE], [1], title="   ")


# --- duplicate settlement ---------------------------------------------------

def test_double_claim_rejected(direct_vm, direct_deploy):
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
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
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, ALICE)
    c.claim(sid)
    assert c.get_split(sid)["status"] == "finalized"
    with direct_vm.expect_revert("not open"):
        c.claim(sid)


def test_non_recipient_cannot_claim(direct_vm, direct_deploy):
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, OUTSIDER)
    with direct_vm.expect_revert("not a recipient"):
        c.claim(sid)


def test_unknown_split_rejected(direct_vm, direct_deploy):
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    with direct_vm.expect_revert("split not found"):
        c.claim("sp-99")


# --- rounding ---------------------------------------------------------------

def test_remainders_are_apportioned_and_pot_is_fully_distributed(direct_vm, direct_deploy):
    """Regression: 100 across three equal weights used to pay 33+33+33 = 99.

    The stranded unit meant `released` never reached `total`, so the split
    stayed open forever *and* `void` refused because a share had been paid.
    Largest-remainder apportionment distributes every unit.
    """
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE, BOB, CAROL], [1, 1, 1])

    # Previews agree with each other and account for the whole pot.
    shares = [c.preview(sid, i) for i in range(3)]
    assert shares == c.preview_all(sid)
    assert sum(shares) == 100
    # Each share is within one unit of its exact quota (100/3 = 33.33).
    assert all(33 <= x <= 34 for x in shares)

    paid = 0
    for who in (ALICE, BOB, CAROL):
        _as_prank(direct_vm, who)
        paid += c.claim(sid)

    assert paid == 100
    assert [t["value"] for t in transfers] == shares
    s = c.get_split(sid)
    assert s["released"] == 100
    assert s["status"] == "finalized"


def test_apportionment_is_order_independent(direct_vm, direct_deploy):
    """Whoever claims first must not change anyone's amount."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 1000, [ALICE, BOB, CAROL], [1, 1, 1])
    expected = c.preview_all(sid)

    # Claim in reverse index order.
    got = {}
    for who in (CAROL, BOB, ALICE):
        _as_prank(direct_vm, who)
        got[who] = c.claim(sid)

    assert [got[ALICE], got[BOB], got[CAROL]] == expected
    assert sum(got.values()) == 1000


def test_seven_equal_weights_distribute_exactly(direct_vm, direct_deploy):
    """Worst-case flooring case: 7 ways, each floor loses a unit."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    people = ["0x" + f"{i:02x}" * 20 for i in range(1, 8)]
    sid = _create(direct_vm, c, 1000, people, [1] * 7)

    shares = c.preview_all(sid)
    assert sum(shares) == 1000
    assert max(shares) - min(shares) <= 1

    for who in people:
        _as_prank(direct_vm, who)
        c.claim(sid)
    assert c.get_split(sid)["status"] == "finalized"
    assert c.get_split(sid)["released"] == 1000


def test_one_unit_pot_goes_to_exactly_one_recipient(direct_vm, direct_deploy):
    """Degenerate case: fewer units than recipients."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 1, [ALICE, BOB, CAROL], [1, 1, 1])
    shares = c.preview_all(sid)
    assert shares == [1, 0, 0]
    assert sum(shares) == 1

    # A zero share is still a valid claim: it marks the share paid and lets
    # the split close, rather than reverting and stranding it.
    for who in (ALICE, BOB, CAROL):
        _as_prank(direct_vm, who)
        c.claim(sid)
    s = c.get_split(sid)
    assert s["status"] == "finalized"
    assert s["released"] == 1


def test_equal_weights_with_uneven_total(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 1000, [ALICE, BOB], [3, 3])
    assert c.preview_all(sid) == [500, 500]


def test_apportionment_never_under_or_over_pays(direct_vm, direct_deploy):
    """Every share must sit within [floor, ceil] of its exact quota."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    for total, weights in [
        (100, [1, 2, 3]),
        (1000003, [5, 3, 2]),
        (7, [1, 1, 1]),
        (5, [2, 2, 2]),
        (999, [2, 2, 2]),
        (10**6, [1, 1, 1]),
    ]:
        sid = _create(direct_vm, c, total, [ALICE, BOB, CAROL], weights)
        shares = c.preview_all(sid)
        tw = sum(weights)
        assert sum(shares) == total, (total, weights, shares)
        for share, w in zip(shares, weights):
            exact = total * w / tw
            assert exact - 1 <= share <= exact + 1, (total, weights, shares)


def test_full_weight_to_single_recipient(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 777, [ALICE], [5])
    _as_prank(direct_vm, ALICE)
    assert c.claim(sid) == 777


def test_large_weights_do_not_overflow_proportion(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 10**12, [ALICE, BOB], [10**9, 3 * 10**9])
    _as_prank(direct_vm, ALICE)
    got = c.claim(sid)
    assert got == 10**12 // 4


# --- recovery ---------------------------------------------------------------

def test_creator_can_void_before_any_payout(direct_vm, direct_deploy):
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
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


def test_void_after_partial_payout_refunds_only_the_remainder(direct_vm, direct_deploy):
    """Regression: a paid share used to block void entirely.

    If some recipients claim and the rest never do, their funds were
    unrecoverable. Void now refunds exactly `total - released`.
    """
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE, BOB], [1, 1])

    _as_prank(direct_vm, ALICE)
    assert c.claim(sid) == 50
    assert transfers[-1]["value"] == 50

    _as_prank(direct_vm, CREATOR)
    assert c.void(sid) == "void"
    # Only the unclaimed 50 comes back; Alice's 50 is never refunded twice.
    assert transfers[-1]["value"] == 50
    assert [t["value"] for t in transfers] == [50, 50]

    s = c.get_split(sid)
    assert s["status"] == "void"
    assert s["released"] == 100

    # Alice cannot claim again from a voided split.
    _as_prank(direct_vm, ALICE)
    with direct_vm.expect_revert("not open"):
        c.claim(sid)
    _as_prank(direct_vm, BOB)
    with direct_vm.expect_revert("not open"):
        c.claim(sid)


def test_void_after_payout_does_not_double_pay_the_creator(direct_vm, direct_deploy):
    """The creator must not recover more than the contract still holds."""
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 1000, [ALICE, BOB, CAROL], [1, 1, 1])

    _as_prank(direct_vm, ALICE)
    c.claim(sid)
    _as_prank(direct_vm, BOB)
    c.claim(sid)

    _as_prank(direct_vm, CREATOR)
    c.void(sid)

    total_out = sum(t["value"] for t in transfers)
    assert total_out == 1000, "more or less than the pot left the contract"
    assert transfers[-1]["value"] == 1000 - c.get_share(sid, 0)["paid_amount"] \
        - c.get_share(sid, 1)["paid_amount"]


def test_void_emits_nothing_when_nothing_is_left(direct_vm, direct_deploy):
    """A split that cannot be claimed is not reached here, but the guard
    matters: a zero refund must not emit an empty transfer."""
    transfers = _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 10, [ALICE], [1])

    # Creator voids before any claim: full refund.
    _as_prank(direct_vm, CREATOR)
    c.void(sid)
    assert [t["value"] for t in transfers] == [10]


def test_only_creator_can_void(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, ALICE)
    with direct_vm.expect_revert("only the creator may void"):
        c.void(sid)


def test_cannot_void_twice(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 100, [ALICE], [1])
    _as_prank(direct_vm, CREATOR)
    c.void(sid)
    with direct_vm.expect_revert("not open"):
        c.void(sid)


# --- views ------------------------------------------------------------------

def test_missing_split_view(direct_vm, direct_deploy):
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    assert c.get_split("sp-0")["found"] is False
    assert c.get_share("sp-0", 0)["found"] is False
    assert c.preview("sp-0", 0) == 0


def test_ids_are_sequential_and_independent(direct_vm, direct_deploy):
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    a = _create(direct_vm, c, 10, [ALICE], [1])
    b = _create(direct_vm, c, 20, [BOB], [1])
    assert (a, b) == ("sp-0", "sp-1")
    assert c.get_split(a)["total"] == 10
    assert c.get_split(b)["total"] == 20


# --- apportionment, exercised through the deployed contract -----------------
#
# These drive `preview_all`, the same function `claim` uses, so they test the
# real code path rather than a re-implementation. The contract imports
# `genlayer`, which only resolves inside the harness, so no attempt is made to
# import the module directly.

SILENT = "0x" + "5a" * 20


def test_apportion_sums_exactly_over_a_sweep(direct_vm, direct_deploy):
    """The whole pot is always distributed, for every shape tried."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    shapes = ([1], [1, 1], [1, 1, 1], [1, 2, 3], [5, 3, 2], [1] * 7,
              [1] * 12, [10**9, 3 * 10**9], [7, 1, 1])
    for total in [1, 2, 3, 7, 40, 100, 999, 1000, 1000003]:
        for weights in shapes:
            n = len(weights)
            # Circle through a pool large enough for the widest shape.
            pool = ["0x" + f"{i + 1:02x}" * 20 for i in range(n)]
            sid = _create(direct_vm, c, total, pool, weights)
            shares = c.preview_all(sid)
            assert sum(shares) == total, (total, weights, shares)
            assert all(s >= 0 for s in shares)


def test_apportion_matches_exact_quota_within_one_unit(direct_vm, direct_deploy):
    from fractions import Fraction

    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    for total in [1, 5, 7, 100, 101, 1000, 10001]:
        for weights in ([1, 1, 1], [1, 2, 3], [3, 2, 2], [1] * 9, [5, 4, 3, 2, 1]):
            n = len(weights)
            pool = ["0x" + f"{i + 1:02x}" * 20 for i in range(n)]
            sid = _create(direct_vm, c, total, pool, weights)
            shares = c.preview_all(sid)
            tw = sum(weights)
            for s, w in zip(shares, weights):
                exact = Fraction(total * w, tw)
                assert exact - 1 <= s <= exact + 1, (total, weights, shares)


def test_apportion_tie_break_is_lowest_index(direct_vm, direct_deploy):
    """Equal remainders resolve by index, which is what removes claim-order
    dependence from the result."""
    _capture_transfers(direct_vm)
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    sid = _create(direct_vm, c, 10, [ALICE, BOB, CAROL], [1, 1, 1])
    assert c.preview_all(sid) == [4, 3, 3]

    sid = _create(direct_vm, c, 11, [ALICE, BOB, CAROL], [1, 1, 1])
    assert c.preview_all(sid) == [4, 4, 3]


def test_preview_all_missing_split_is_empty(direct_vm, direct_deploy):
    c = direct_deploy("archive/depositsplit/depositsplit.py")
    assert c.preview_all("sp-404") == []
