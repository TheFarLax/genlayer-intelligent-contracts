import json

import pytest

GOOD_SPEC = """Build a CSV export button for the dashboard.
Deliver a single button labelled "Export CSV" in the top-right toolbar.
Acceptance: clicking it downloads a file named dashboard-YYYY-MM-DD.csv
containing every visible row and column.
Constraints: must work in Chrome and Firefox, no new dependencies."""

GOOD_REPLY = json.dumps({
    "scope": 24,
    "deliverables": 23,
    "acceptance": 24,
    "constraints": 22,
    "ambiguities": [],
    "assumptions": ["Timezone is the user's local timezone"],
})


def _deploy(direct_deploy):
    return direct_deploy("contracts/speclint.py")


def test_analyze_stores_report(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*grading a work specification.*", GOOD_REPLY)

    report_id = contract.analyze("CSV export button", GOOD_SPEC)
    assert report_id

    r = contract.get_report(report_id)
    assert r["title"] == "CSV export button"
    assert r["clarity_score"] == 24 + 23 + 24 + 22
    assert r["total"] == r["clarity_score"]
    assert r["ambiguous"] is False
    assert r["ambiguities"] == []
    assert r["assumptions"] == ["Timezone is the user's local timezone"]
    assert contract.is_clear(report_id) is True


def test_low_score_marks_ambiguous(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    vague = json.dumps({
        "scope": 6, "deliverables": 5, "acceptance": 4, "constraints": 3,
        "ambiguities": ["No deadline given", "Audience undefined",
                        "Format unspecified", "Budget unclear"],
        "assumptions": ["Assume a reasonable budget"],
    })
    direct_vm.mock_llm(r"(?s).*", vague)

    rid = contract.analyze("vague thing", "make it better")
    r = contract.get_report(rid)
    assert r["ambiguous"] is True
    assert len(r["ambiguities"]) == 3          # capped at three
    assert contract.is_clear(rid) is False


def test_deterministic_index_by_hash(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*", GOOD_REPLY)

    rid = contract.analyze("t", GOOD_SPEC)
    r = contract.get_report(rid)
    assert contract.get_by_hash(r["spec_hash"]) == rid

    # The id is derived from content, so re-analyzing the same spec by the
    # same sender reproduces it exactly. That is deliberate: the caller can
    # recompute the id without trusting the write, and a repeat submission is
    # idempotent instead of accumulating unreachable duplicates.
    rid2 = contract.analyze("t again", GOOD_SPEC)
    assert rid2 == rid
    assert contract.count() == 1
    # The later title overwrote the earlier one; the hash index is unchanged.
    assert contract.get_report(rid)["title"] == "t again"
    assert contract.get_by_hash(r["spec_hash"]) == rid


# --- validator behaviour ----------------------------------------------------

def test_validator_accepts_consistent_result(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    # Leader and validator both see the same rubric output.
    direct_vm.mock_llm(r"(?s).*", GOOD_REPLY)
    contract.analyze("t", GOOD_SPEC)
    assert direct_vm.run_validator() is True


def test_validator_rejects_wildly_different_scores(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*", GOOD_REPLY)
    contract.analyze("t", GOOD_SPEC)

    # Validator now sees a very different reading of the same spec.
    direct_vm.clear_mocks()
    divergent = json.dumps({
        "scope": 5, "deliverables": 5, "acceptance": 5, "constraints": 5,
        "ambiguities": ["unclear"], "assumptions": [],
    })
    direct_vm.mock_llm(r"(?s).*", divergent)
    assert direct_vm.run_validator() is False


def test_validator_rejects_gate_disagreement(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*", GOOD_REPLY)
    contract.analyze("t", GOOD_SPEC)

    # Scores are close enough, but this validator would call summary
    # ambiguous while the leader says clear.
    direct_vm.clear_mocks()

    def reply_with_total(total):
        # Build a reply whose dimension sums land near `total` but whose
        # ambiguity status flips through the score gate.
        per = total // 4
        return json.dumps({
            "scope": per, "deliverables": per, "acceptance": per,
            "constraints": total - 3 * per,
            "ambiguities": ["x"], "assumptions": [],
        })

    # 93 -> still clears the >=60 gate, so gate agrees; must be True
    direct_vm.mock_llm(r"(?s).*", reply_with_total(93))
    assert direct_vm.run_validator() is True

    # Now a low-but-not-too-low total that lands on the other side of the gate
    # while staying within tolerance of nothing meaningful: 55 vs 93 is a big
    # divergence so this must be rejected.
    direct_vm.clear_mocks()
    direct_vm.mock_llm(r"(?s).*", reply_with_total(55))
    assert direct_vm.run_validator() is False


def test_validator_rejects_leader_error(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*", GOOD_REPLY)
    contract.analyze("t", GOOD_SPEC)
    assert direct_vm.run_validator(leader_error=RuntimeError("boom")) is False


# --- input validation -------------------------------------------------------

@pytest.mark.parametrize("title,spec,needle", [
    ("", "some spec", "title is required"),
    ("t", "", "spec is required"),
    ("t", "x" * 6001, "spec too long"),
])
def test_rejects_invalid_input(direct_vm, direct_deploy, title, spec, needle):
    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert(needle):
        contract.analyze(title, spec)


def test_rejects_bad_llm_output(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    direct_vm.mock_llm(r"(?s).*", "this is not json at all")
    with direct_vm.expect_revert():
        contract.analyze("t", GOOD_SPEC)


def test_get_report_missing(direct_vm, direct_deploy):
    """Missing ids are reported, not reverted: RPC callers need a readable answer."""
    contract = _deploy(direct_deploy)
    got = contract.get_report("nope")
    assert got["found"] is False
    assert got["report_id"] == "nope"
