import json

import pytest

PRICE_URL = "https://api.example.com/price"
FUTURE = 1956528000  # 2032-01-01T00:00:00Z as epoch seconds
PAST = 946684800  # 2000-01-01
AFTER = "2033-01-01T00:00:00Z"
LATER = "2034-01-01T00:00:00Z"
LATERISH = "2035-01-01T00:00:00Z"


def _price_body(price):
    return json.dumps({"symbol": "ETHUSD", "price": price})


def _block_now():
    """Current simulated block time, read the same way the contract reads it."""
    import datetime

    from gltest.direct import wasi_mock

    vm = wasi_mock.get_vm()
    return int(
        datetime.datetime.fromisoformat(vm._datetime.replace("Z", "+00:00")).timestamp()
    )


def _mock(direct_vm, price, status=200):
    direct_vm.mock_web(
        r".*api\.example\.com/price.*", {"status": status, "body": _price_body(price)}
    )
    direct_vm.mock_llm(
        r"(?s).*Extract the CURRENT PRICE.*", json.dumps({"price": price})
    )


def _reg(c, direction="up", threshold=3_000_000_000, deadline="1 day"):
    """threshold is in integer micro-units (3000.000000 USD -> 3e9).

    `deadline="1 day"` means one day after the CURRENT block time; the
    contract rejects past deadlines, and tests warp the clock freely.
    """
    if isinstance(deadline, str):
        # A `deadline` string means "this many days after the current block time".
        deadline = _block_now() + int(deadline.split()[0]) * 86400
    return c.register("ETH", direction, threshold, int(deadline), PRICE_URL, "x")


def _sender_hex(direct_vm):
    s = direct_vm.sender
    return s.as_hex if hasattr(s, "as_hex") else "0x" + s.hex()


# --- registration -----------------------------------------------------------


def test_register(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    assert fid == "fc-0"
    f = c.get_forecast(fid)
    assert f["status"] == "pending"
    assert f["asset"] == "ETH"
    assert f["direction"] == "up"
    assert f["threshold_micros"] == 3_000_000_000
    assert f["observed_micros"] is None
    assert c.count() == 1


def test_ids_are_sequential(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    assert _reg(c) == "fc-0"
    assert _reg(c) == "fc-1"


# --- settlement -------------------------------------------------------------


def test_up_hit_and_miss(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c, "up", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    assert c.settle(fid) == "hit"
    got = c.get_forecast(fid)
    assert got["status"] == "hit"
    assert got["observed_micros"] == 3_500_000_000
    assert got["evidence_hash"] != ""
    assert got["settled_at"] > 0

    # Registered and settled from a later block: a second call on the same
    # contract must not disturb the first forecast's recorded outcome.
    direct_vm.warp(LATER)
    fid2 = _reg(c, "up", 4_000_000_000, deadline="2 days")
    direct_vm.warp(LATERISH)
    direct_vm.clear_mocks()
    _mock(direct_vm, 3500.0)
    assert c.settle(fid2) == "miss"
    assert c.get_forecast(fid)["status"] == "hit"


def test_down_hit_and_miss(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c, "down", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 2500.0)
    assert c.settle(fid) == "hit"

    direct_vm.warp(LATER)
    fid2 = _reg(c, "down", 1_000_000_000, deadline="2 days")
    direct_vm.warp(LATERISH)
    direct_vm.clear_mocks()
    _mock(direct_vm, 2500.0)
    assert c.settle(fid2) == "miss"
    assert c.get_forecast(fid)["status"] == "hit"


def test_exact_threshold_is_a_miss(direct_vm, direct_deploy):
    """Equality never pays out; both directions use a strict comparison."""
    c = direct_deploy("contracts/forecastledger.py")
    up = _reg(c, "up", 3_000_000_000)
    down = _reg(c, "down", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3000.0)
    assert c.settle(up) == "miss"
    direct_vm.clear_mocks()
    _mock(direct_vm, 3000.0)
    assert c.settle(down) == "miss"


def test_cannot_settle_before_deadline(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    with direct_vm.expect_revert("not yet due"):
        c.settle(fid)


def test_cannot_settle_twice(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    assert c.settle(fid) == "hit"
    with direct_vm.expect_revert("already settled"):
        c.settle(fid)


def test_source_down_leaves_pending_and_retryable(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    direct_vm.mock_web(r".*", {"status": 503, "body": "down"})

    with direct_vm.expect_revert("source unavailable"):
        c.settle(fid)
    # Critically: not settled, still retryable.
    assert c.get_forecast(fid)["status"] == "pending"

    direct_vm.clear_mocks()
    _mock(direct_vm, 3500.0)
    assert c.settle(fid) == "hit"


def test_unparseable_price_leaves_pending(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    direct_vm.mock_web(r".*", {"status": 200, "body": "not a price page"})
    direct_vm.mock_llm(r"(?s).*Extract the CURRENT PRICE.*", json.dumps({"price": None}))
    with direct_vm.expect_revert("no price in response"):
        c.settle(fid)
    assert c.get_forecast(fid)["status"] == "pending"


def test_settle_unknown(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    with direct_vm.expect_revert("forecast not found"):
        c.settle("nope")


# --- per-caller accounting --------------------------------------------------


def test_caller_ids_listed(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid1 = _reg(c)
    fid2 = _reg(c)
    assert c.get_caller_ids(_sender_hex(direct_vm)) == [fid1, fid2]


def test_get_record_counts(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    hit = _reg(c, "up", 3_000_000_000)
    miss = _reg(c, "up", 9_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    assert c.settle(hit) == "hit"
    direct_vm.clear_mocks()
    _mock(direct_vm, 3500.0)
    assert c.settle(miss) == "miss"

    rec = c.get_record(_sender_hex(direct_vm))
    assert rec["hits"] == 1
    assert rec["misses"] == 1
    assert rec["settled"] == 2
    assert rec["hit_rate_bps"] == 5000


def test_get_record_for_unknown_caller(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    rec = c.get_record("0x" + "11" * 20)
    assert rec["settled"] == 0
    assert rec["hit_rate_bps"] == 0


# --- validator --------------------------------------------------------------


def test_validator_accepts_close_price(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    c.settle(fid)

    # Validator sees a price 0.1% away: inside tolerance -> accept.
    direct_vm.clear_mocks()
    _mock(direct_vm, 3503.5)
    assert direct_vm.run_validator() is True


def test_validator_rejects_price_drift(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    c.settle(fid)

    # 2% away: outside tolerance -> reject.
    direct_vm.clear_mocks()
    _mock(direct_vm, 3570.0)
    assert direct_vm.run_validator() is False


def test_validator_rejects_unreadable_source_when_leader_ok(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    c.settle(fid)

    direct_vm.clear_mocks()
    direct_vm.mock_web(r".*", {"status": 500, "body": "err"})
    assert direct_vm.run_validator() is False


def test_validator_rejects_same_source_unreadable_leader(direct_vm, direct_deploy):
    """If both see a dead source, the leader's 'unavailable' reading is fine."""
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    direct_vm.mock_web(r".*", {"status": 503, "body": "down"})
    with direct_vm.expect_revert("source unavailable"):
        c.settle(fid)
    assert direct_vm.run_validator() is True


def test_validator_rejects_leader_error(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    c.settle(fid)
    assert direct_vm.run_validator(leader_error=RuntimeError("x")) is False


# --- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "args,needle",
    [
        (("", "up", 1_000_000, FUTURE, PRICE_URL, ""), "asset is required"),
        (("ETH", "sideways", 1_000_000, FUTURE, PRICE_URL, ""), "direction must be"),
        (("ETH", "up", 1_000_000, FUTURE, "", ""), "source_url is required"),
        (("ETH", "up", 1_000_000, FUTURE, "ftp://x", ""), "must be http"),
        (("ETH", "up", 0, FUTURE, PRICE_URL, ""), "threshold must be positive"),
        (("ETH", "up", -5, FUTURE, PRICE_URL, ""), "threshold must be positive"),
        (("ETH", "up", 1_000_000, PAST, PRICE_URL, ""), "deadline must be in the future"),
    ],
)
def test_register_validation(direct_vm, direct_deploy, args, needle):
    c = direct_deploy("contracts/forecastledger.py")
    with direct_vm.expect_revert(needle):
        c.register(*args)


def test_register_normalizes_case(direct_vm, direct_deploy):
    c = direct_deploy("contracts/forecastledger.py")
    fid = c.register("  eth ", "UP", 1_000_000, FUTURE, PRICE_URL, "n")
    got = c.get_forecast(fid)
    assert got["asset"] == "ETH"
    assert got["direction"] == "up"


# --- outcome feed wiring ----------------------------------------------------

FEED = "0x" + "ab" * 20


def _capture_posts(direct_vm):
    """Record cross-contract messages the contract emits.

    The direct-mode VM has no peer contract to call, so the emission is
    captured through the gl_call hook instead. This verifies that settlement
    posts exactly one `append` to the configured feed with the decided outcome.
    """
    seen = []

    def hook(vm, request):
        if isinstance(request, dict) and "PostMessage" in request:
            seen.append(request["PostMessage"])
            return {"ok": {}}
        return None

    direct_vm._gl_call_hook = hook
    return seen


def test_settle_publishes_to_configured_feed(direct_vm, direct_deploy):
    seen = _capture_posts(direct_vm)
    c = direct_deploy("contracts/forecastledger.py", FEED)
    fid = _reg(c, "up", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)

    assert c.settle(fid) == "hit"

    assert len(seen) == 1
    post = seen[0]
    posted_addr = post["address"]
    posted_addr = posted_addr.as_hex if hasattr(posted_addr, "as_hex") else str(posted_addr)
    assert posted_addr.lower() == FEED
    assert post["on"] == "finalized"
    calldata_ = post["calldata"]
    assert calldata_["method"] == "append"
    args = calldata_["args"]
    assert args[0] == fid
    assert args[1] == "ETH"
    assert args[2] == "hit"
    assert args[3] == 3_500_000_000


def test_unconfigured_ledger_emits_nothing(direct_vm, direct_deploy):
    seen = _capture_posts(direct_vm)
    c = direct_deploy("contracts/forecastledger.py")
    fid = _reg(c, "up", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)
    assert c.settle(fid) == "hit"
    assert seen == []


def test_feed_failure_does_not_block_settlement(direct_vm, direct_deploy):
    """A dead or misconfigured feed must not strand a settled forecast."""
    def boom(vm, request):
        if isinstance(request, dict) and "PostMessage" in request:
            raise RuntimeError("feed unreachable")
        return None

    direct_vm._gl_call_hook = boom
    c = direct_deploy("contracts/forecastledger.py", FEED)
    fid = _reg(c, "up", 3_000_000_000)
    direct_vm.warp(AFTER)
    _mock(direct_vm, 3500.0)

    assert c.settle(fid) == "hit"
    assert c.get_forecast(fid)["status"] == "hit"
