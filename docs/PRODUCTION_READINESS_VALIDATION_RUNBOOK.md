# Production readiness validation runbook

This is a read-only, offline validation procedure. It does not make the system production-ready.

## Validate an exact pack

Use an absolute path; `latest`, directory discovery and relative paths are rejected:

```bash
island-quant validate-production-readiness \
  --pack "$PWD/docs/readiness/phase4c_synthetic_readiness_pack.json"
```

Exit code `0` means every gate for the declared scope passed. Exit code `3` means a deterministic
no-go report was produced. Exit code `2` means the pack or schema is invalid. The command reads only
the given regular, non-symlink JSON file. It does not read credentials, configuration, runtime
databases, caches or the network, and it creates no files.

The included pack is synthetic, offline, not provider behavior, not licensed production data and
not live-ready. Its successful evaluation proves only evaluator conformance. Even then the report
keeps `production_admission=no_go` and `live_trading_ready=false`.

`readiness/phase4c_production_readiness.json` is the checked-in deterministic example no-go report
for the current project state. It identifies real missing evidence and decisions; it is not a pack
that can be promoted or executed.

## No-go handling

For each blocker, route `owner`, ask for `required_evidence`, and execute only `next_safe_step`.
Do not replace `unknown` with an inference. Expired evidence must be retrieved and reviewed again.
Any provider disagreement, incomplete PIT timestamp, coverage below 100%, stream fault, license
uncertainty, missing deployment decision or erased broker unknown keeps the gate closed.

After changing a pack, run the Phase 4 focused tests and compare canonical output and
`report_checksum`. Preserve the exact evaluated pack with the report; never relabel synthetic
evidence as official or contract evidence.
