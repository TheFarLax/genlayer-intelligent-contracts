import pytest

# The tests drive the contract through the direct-mode VM, whose sender is the
# deployer unless overridden. Deploying with the current sender as producer
# therefore lets the test call `append` as the producer, and any other sender
# exercises the rejection path.

FUTURE = 1956528000  # 2032-01-01T00:00:00Z


def _sender_hex(direct_vm):
    s = direct_vm.sender
    return s.as_hex if hasattr(s, "as_hex") else "0x" + s.hex()


def _deploy(direct_deploy, direct_vm, producer=None):
    return direct_deploy(
        "contracts/outcomefeed.py", producer or _sender_hex(direct_vm)
    )


def _app(c, ref="fc-0", subject="ETH", status="hit", micros=3_100_000_000,
         evidence="a1b2", reason="observed above threshold"):
    return c.append(ref, subject, status, micros, evidence, reason)


# --- append -----------------------------------------------------------------

def test_append_stores_entry(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    key = _app(c)

    e = c.get(key)
    assert e["found"] is True
    assert e["ref"] == "fc-0"
    assert e["subject"] == "ETH"
    assert e["status"] == "hit"
    assert e["observed_micros"] == 3_100_000_000
    assert e["evidence_hash"] == "a1b2"
    assert c.count() == 1
    assert c.key_for("fc-0") == key


def test_subject_is_upper_cased(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    key = _app(c, subject="eth")
    assert c.get(key)["subject"] == "ETH"
    # Both spellings resolve to the same tally bucket.
    assert c.subject_tally("ETH")["settled"] == 1
    assert c.subject_tally("eth")["settled"] == 1


# --- idempotency ------------------------------------------------------------

def test_duplicate_ref_is_a_noop(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    first = _app(c, ref="fc-1", status="hit")

    # A retrying producer re-sends the same outcome. It must converge, not
    # append a second row that would double-count in every tally.
    again = _app(c, ref="fc-1", status="miss", micros=1)
    assert again == first
    assert c.count() == 1
    assert c.get(first)["status"] == "hit"
    assert c.subject_tally("ETH")["hits"] == 1
    assert c.subject_tally("ETH")["misses"] == 0


def test_same_ref_different_subject_still_dedupes_on_ref(direct_vm, direct_deploy):
    """Refs are producer-unique ids, so the subject must not fork a second row."""
    c = _deploy(direct_deploy, direct_vm)
    first = _app(c, ref="fc-2", subject="ETH")
    second = _app(c, ref="fc-2", subject="BTC")
    assert first == second
    assert c.count() == 1


# --- access control ---------------------------------------------------------

def test_only_producer_may_append(direct_vm, direct_deploy):
    other = "0x" + "11" * 20
    c = _deploy(direct_deploy, direct_vm, producer=other)
    with direct_vm.expect_revert("only the producer may append"):
        _app(c)


def test_zero_producer_rejected_at_deploy(direct_vm, direct_deploy):
    with direct_vm.expect_revert("zero address"):
        direct_deploy("contracts/outcomefeed.py",
                      "0x0000000000000000000000000000000000000000")


def test_empty_producer_rejected_at_deploy(direct_vm, direct_deploy):
    with direct_vm.expect_revert("producer is required"):
        direct_deploy("contracts/outcomefeed.py", "")


# --- invalid input ----------------------------------------------------------

@pytest.mark.parametrize("kwargs,needle", [
    ({"ref": ""}, "ref is required"),
    ({"subject": ""}, "subject is required"),
    ({"status": "void"}, "status must be hit or miss"),
    ({"status": "HIT!"}, "status must be hit or miss"),
    ({"status": "pending"}, "status must be hit or miss"),
    ({"micros": -1}, "must not be negative"),
    ({"reason": "x" * 281}, "reason too long"),
    ({"ref": "r" * 121}, "ref too long"),
])
def test_rejects_invalid_input(direct_vm, direct_deploy, kwargs, needle):
    c = _deploy(direct_deploy, direct_vm)
    call = dict(ref="fc-9", subject="ETH", status="hit", micros=1,
                evidence="", reason="ok")
    call.update(kwargs)
    with direct_vm.expect_revert(needle):
        c.append(call["ref"], call["subject"], call["status"],
                 call["micros"], call["evidence"], call["reason"])
    # A rejected append must leave the feed untouched.
    assert c.count() == 0


# --- tallies ----------------------------------------------------------------

def test_tally_mixes_hits_and_misses(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    _app(c, ref="f1", subject="ETH", status="hit")
    _app(c, ref="f2", subject="ETH", status="hit")
    _app(c, ref="f3", subject="ETH", status="miss")
    _app(c, ref="f4", subject="BTC", status="miss")

    eth = c.subject_tally("ETH")
    assert eth == {"subject": "ETH", "hits": 2, "misses": 1,
                   "settled": 3, "hit_rate_bps": 6666}
    btc = c.subject_tally("BTC")
    assert btc["settled"] == 1 and btc["hit_rate_bps"] == 0
    assert c.subject_tally("DOGE")["settled"] == 0
    assert c.count() == 4


def test_zero_observation_is_marked_absent(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    key = _app(c, micros=0)
    assert c.get(key)["observed_micros"] is None


def test_missing_key_is_reported(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    assert c.get("nope")["found"] is False
    assert c.key_for("nope") == ""
    assert c.get_producer().lower() == _sender_hex(direct_vm).lower()


# --- one-shot authorization --------------------------------------------------

def test_producer_authorizes_one_writer(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    writer = "0x" + "77" * 20
    assert c.authorize(writer) is True
    assert c.get_authorized().lower() == writer

    # A second authorization is refused, even by the producer.
    with direct_vm.expect_revert("already exists"):
        c.authorize("0x" + "88" * 20)


def test_only_producer_may_authorize(direct_vm, direct_deploy):
    other = "0x" + "11" * 20
    c = _deploy(direct_deploy, direct_vm, producer=other)
    with direct_vm.expect_revert("only the producer may authorize"):
        c.authorize(_sender_hex(direct_vm))


def test_authorize_rejects_bad_writers(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    with direct_vm.expect_revert("writer is required"):
        c.authorize("")
    with direct_vm.expect_revert("zero address"):
        c.authorize("0x0000000000000000000000000000000000000000")
    with direct_vm.expect_revert("must not be the producer"):
        c.authorize(_sender_hex(direct_vm))


def test_authorized_writer_can_append(direct_vm, direct_deploy):
    """The whole point: the ledger contract, authorized once, may append."""
    c = _deploy(direct_deploy, direct_vm)
    writer = "0x" + "77" * 20
    c.authorize(writer)

    # Prank as the authorized writer contract address.
    direct_vm.sender = writer
    key = _app(c, ref="fc-lw", subject="ETH", status="hit")
    assert c.get(key)["found"] is True
    assert c.get(key)["status"] == "hit"


def test_still_refuses_everyone_else(direct_vm, direct_deploy):
    c = _deploy(direct_deploy, direct_vm)
    c.authorize("0x" + "77" * 20)
    direct_vm.sender = "0x" + "99" * 20
    with direct_vm.expect_revert("only the producer may append"):
        _app(c)


def test_unauthorized_before_authorization(direct_vm, direct_deploy):
    other = "0x" + "11" * 20
    c = _deploy(direct_deploy, direct_vm, producer=other)
    with direct_vm.expect_revert("only the producer may append"):
        _app(c)
