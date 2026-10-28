import json

import pytest

URL = "https://example.com/status"
CRITERIA = "The page must state that all systems are operational."


def _mock_web(direct_vm, body, status=200):
    direct_vm.mock_web(r".*example\.com/status.*", {
        "status": status,
        "body": body,
    })


def _mock_llm(direct_vm, verdict, reason="because"):
    direct_vm.mock_llm(r"(?s).*verifying whether an ON-CHAIN CLAIM.*", json.dumps({
        "verdict": verdict, "reason": reason,
    }))


def _reg(contract, claim="Service is operational", interval=60):
    return contract.register(claim, URL, CRITERIA, interval)


def test_register(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    assert cid == "claim-0"
    got = c.get_claim(cid)
    assert got["verdict"] == "unchecked"
    assert got["source_url"] == URL
    assert got["check_count"] == 0
    assert got["has_broken"] is False
    assert c.get_verdict(cid) == "unchecked"


def test_holds(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds", "page says all systems operational")

    assert c.recheck(cid) == "holds"
    got = c.get_claim(cid)
    assert got["verdict"] == "holds"
    assert got["check_count"] == 1
    assert got["evidence_hash"] != ""
    assert got["reason"] == "page says all systems operational"


def test_broken_is_sticky_and_timestamped(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "Major outage")
    _mock_llm(direct_vm, "broken")
    assert c.recheck(cid) == "broken"
    first = c.get_claim(cid)
    assert first["has_broken"] is True
    assert first["broken_at"] > 0

    # Advance past the cooldown, recover to holds: has_broken stays true.
    direct_vm.warp("2030-01-01T00:00:00Z")
    direct_vm.clear_mocks()
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    assert c.recheck(cid) == "holds"
    second = c.get_claim(cid)
    assert second["verdict"] == "holds"
    assert second["has_broken"] is True
    assert second["broken_at"] == first["broken_at"]


def test_unreachable_source_is_undeterminable(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "server error", status=503)
    # No LLM mock: the contract must not reach the model at all.
    assert c.recheck(cid) == "undeterminable"
    got = c.get_claim(cid)
    assert got["verdict"] == "undeterminable"
    assert "could not be fetched" in got["reason"]
    assert got["evidence_hash"] == ""


def test_cooldown_blocks_immediate_recheck(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c, interval=60)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)
    with direct_vm.expect_revert("recheck too soon"):
        c.recheck(cid)


def test_due_for_check(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c, interval=3600)
    assert c.due_for_check(cid) is True     # never checked
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)
    assert c.due_for_check(cid) is False    # just checked
    direct_vm.warp("2030-01-01T00:00:00Z")
    assert c.due_for_check(cid) is True     # interval elapsed


# --- validator --------------------------------------------------------------

def test_validator_accepts_matching_verdict(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)
    assert direct_vm.run_validator() is True


def test_validator_rejects_disagreement(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)

    # Validator now sees evidence that contradicts the leader.
    direct_vm.clear_mocks()
    _mock_web(direct_vm, "Major outage in progress")
    _mock_llm(direct_vm, "broken")
    assert direct_vm.run_validator() is False


def test_validator_requires_undeterminable_when_source_down(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)

    # Source unreachable for the validator -> leader's "holds" is not defensible.
    direct_vm.clear_mocks()
    _mock_web(direct_vm, "down", status=500)
    assert direct_vm.run_validator() is False


def test_validator_rejects_leader_error(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    _mock_llm(direct_vm, "holds")
    c.recheck(cid)
    assert direct_vm.run_validator(leader_error=RuntimeError("boom")) is False


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("args,needle", [
    (("", URL, CRITERIA, 60), "claim is required"),
    (("x", "", CRITERIA, 60), "source_url is required"),
    (("x", "ftp://nope", CRITERIA, 60), "must be http"),
    (("x", URL, "", 60), "criteria is required"),
    (("x", URL, CRITERIA, 5), "interval_secs must be"),
])
def test_register_validation(direct_vm, direct_deploy, args, needle):
    c = direct_deploy("contracts/claimpin.py")
    with direct_vm.expect_revert(needle):
        c.register(*args)


def test_recheck_unknown_claim(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    with direct_vm.expect_revert("claim not found"):
        c.recheck("nope")


def test_rejects_garbage_verdict(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    cid = _reg(c)
    _mock_web(direct_vm, "All systems operational")
    direct_vm.mock_llm(r"(?s).*", json.dumps({"verdict": "maybe", "reason": "?"}))
    with direct_vm.expect_revert():
        c.recheck(cid)


def test_get_claim_missing_reports_absence(direct_vm, direct_deploy):
    c = direct_deploy("contracts/claimpin.py")
    got = c.get_claim("cp-404")
    assert got["found"] is False
    assert got["claim_id"] == "cp-404"
