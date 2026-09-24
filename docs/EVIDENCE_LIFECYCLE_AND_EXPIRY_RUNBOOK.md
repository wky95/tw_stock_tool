# Evidence lifecycle, expiry, and comparison runbook

Evidence moves through retrieved, reviewed, current, expiring, expired, superseded, and removed
states. Retrieval/review never changes the source checksum; a changed document receives reviewed
metadata and comparison. Expired evidence is no-go and must be retrieved and reviewed again. The
tool flags evidence expiring within 30 days but does not fetch or renew it.

Compare two exact, absolute, checksum-valid draft files:

```bash
island-quant compare-production-readiness \
  --previous /exact/previous-draft.json \
  --candidate /exact/candidate-draft.json
```

The canonical report is ordered by evidence/decision ID and includes added, removed, changed fields,
new expiry, near expiry, `unknown -> approved/rejected`, and `approved -> unknown/rejected` changes.
Critical checksum, URL, document version, owner, or expiry change is a regression pending review.
Coverage decline, rollback-license loss, broker-blocker removal, or operator/security approval loss
is also a regression. Evidence reduction can never improve admission. Report checksum covers every
field except itself; repeated comparison of the same inputs is byte-identical.

Comparison never chooses a provider, resolves a disagreement, renews evidence, edits either draft,
or changes runtime/configuration state. Both output status and live readiness remain no-go.
