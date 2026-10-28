# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import datetime
from dataclasses import dataclass

from genlayer import *

# --- Outcome states ---------------------------------------------------------
HIT = "hit"
MISS = "miss"
VOID = "void"

# Only a forecast status that names a decided outcome may be appended.
DECIDED = (HIT, MISS)

MAX_SUBJECT_CHARS = 40
MAX_REASON_CHARS = 280
MAX_EVIDENCE_CHARS = 80

# The one contract this feed accepts entries from. Immutable after deploy, so
# a compromised or mistyped producer cannot inject into a feed that consumers
# have already pinned.
_MAX_NO_PRODUCER = "0x0000000000000000000000000000000000000000"


def _now() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


@allow_storage
@dataclass
class Outcome:
    seq: u256
    producer: Address
    ref: str
    subject: str
    status: str
    observed_micros: u256
    has_observed: bool
    evidence_hash: str
    reason: str
    appended_at: u256


class OutcomeFeed(gl.Contract):
    entries: TreeMap[str, Outcome]
    # ref -> entry key. Presence of the key is the idempotency gate: a second
    # append for the same (producer, ref) is a no-op, never a duplicate row.
    by_ref: TreeMap[str, str]
    # subject -> DynArray[entry key], so a consumer can read one asset's
    # history without scanning the whole feed.
    by_subject: TreeMap[str, DynArray[str]]
    producer: Address
    total: u256

    def __init__(self, producer: str):
        # A zero producer would let anyone append. Refusing at deploy time is
        # cheaper and safer than discovering it on the first write.
        if not producer:
            raise gl.vm.UserError("producer is required")
        self.producer = Address(producer)
        if self.producer == Address(_MAX_NO_PRODUCER):
            raise gl.vm.UserError("producer must not be the zero address")
        self.total = u256(0)

    # -- append -------------------------------------------------------------

    @gl.public.write
    def append(
        self,
        ref: str,
        subject: str,
        status: str,
        observed_micros: int,
        evidence_hash: str,
        reason: str,
    ) -> str:
        """Append one decided outcome.

        Idempotent on `ref`: the first append for a ref wins and every later
        append for the same ref returns the existing key without writing. That
        matters because the producer calls this from a settlement path that may
        be retried, and duplicated outcomes would silently corrupt any hit-rate
        computed from the feed.

        `observed_micros` uses the same integer micro-unit convention as the
        producer: 3000.50 USD is 3000500000.
        """
        if gl.message.sender_address != self.producer:
            raise gl.vm.UserError("only the producer may append")

        ref = (ref or "").strip()
        subject = (subject or "").strip().upper()
        status = (status or "").strip().lower()
        evidence_hash = (evidence_hash or "").strip().lower()
        reason = (reason or "").strip()

        if ref == "":
            raise gl.vm.UserError("ref is required")
        if len(ref) > 120:
            raise gl.vm.UserError("ref too long")
        if subject == "":
            raise gl.vm.UserError("subject is required")
        if len(subject) > MAX_SUBJECT_CHARS:
            raise gl.vm.UserError("subject too long")
        if status not in DECIDED:
            raise gl.vm.UserError("status must be hit or miss")
        if len(reason) > MAX_REASON_CHARS:
            raise gl.vm.UserError("reason too long")
        if len(evidence_hash) > MAX_EVIDENCE_CHARS:
            raise gl.vm.UserError("evidence_hash too long")

        observed_micros = int(observed_micros)
        if observed_micros < 0:
            raise gl.vm.UserError("observed_micros must not be negative")

        key = f"{subject}:{ref}"
        existing = self.by_ref.get(ref)
        if existing is not None:
            # Already recorded. Return the canonical key so a retrying caller
            # can confirm convergence rather than guessing whether it landed.
            return existing

        n = int(self.total)
        entry = Outcome(
            seq=u256(n),
            producer=self.producer,
            ref=ref,
            subject=subject,
            status=status,
            observed_micros=u256(observed_micros),
            has_observed=observed_micros > 0,
            evidence_hash=evidence_hash,
            reason=reason,
            appended_at=u256(_now()),
        )
        self.entries[key] = entry
        self.by_ref[ref] = key
        self.by_subject.get_or_insert_default(subject).append(key)
        self.total = u256(n + 1)
        return key

    # -- views --------------------------------------------------------------

    @gl.public.view
    def get(self, key: str) -> dict:
        e = self.entries.get(key)
        if e is None:
            return {"found": False, "key": key}
        return {
            "found": True,
            "key": key,
            "seq": int(e.seq),
            "producer": e.producer.as_hex,
            "ref": e.ref,
            "subject": e.subject,
            "status": e.status,
            "observed_micros": int(e.observed_micros) if e.has_observed else None,
            "evidence_hash": e.evidence_hash,
            "reason": e.reason,
            "appended_at": int(e.appended_at),
        }

    @gl.public.view
    def key_for(self, ref: str) -> str:
        """Canonical key for a producer-side ref, or "" if never appended."""
        return self.by_ref.get(ref, "")

    @gl.public.view
    def subject_tally(self, subject: str) -> dict:
        subject = (subject or "").strip().upper()
        hits = 0
        misses = 0
        for key in list(self.by_subject.get(subject, [])):
            e = self.entries.get(key)
            if e is None:
                continue
            if e.status == HIT:
                hits += 1
            elif e.status == MISS:
                misses += 1
        settled = hits + misses
        return {
            "subject": subject,
            "hits": hits,
            "misses": misses,
            "settled": settled,
            "hit_rate_bps": (hits * 10000) // settled if settled > 0 else 0,
        }

    @gl.public.view
    def count(self) -> u256:
        return self.total

    @gl.public.view
    def get_producer(self) -> str:
        return self.producer.as_hex
