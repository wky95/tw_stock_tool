# Shioaji official capability audit

Research date: **2026-09-24 (Asia/Taipei)**. Sources are official SinoPac/Shioaji documentation and
official PyPI package metadata only. No package was installed, no account was used, no login was
attempted, and no broker endpoint was contacted.

Pinned research target: latest PyPI release **1.7.6**, uploaded 2026-09-22. Documentation is mutable;
the adapter milestone must re-check every source and pin an exact version.

| Capability | Official evidence | Audit result |
|---|---|---|
| Python versions | PyPI metadata says `Requires-Python >=3.7`; 1.7.6 publishes CPython 3.7 ABI3 and CPython 3.14 free-threaded wheels. | Supported range is metadata-confirmed; exact Island Quant Python 3.12 runtime remains untested. |
| macOS Apple Silicon | 1.7.6 has `macosx_11_0_arm64` wheels. | Distribution exists; runtime/certificate behavior on the target host is unverified. |
| Installation/native dependencies | Official install is `pip install shioaji`; 1.7.6 publishes wheels and no source distribution. Metadata declares only optional `uvloop` for the speed extra. Release notes say 1.5 moved to a Rust implementation. | Native contents and target-host runtime behavior require an isolated install audit. |
| Account/API eligibility | A SinoPac account, separate stock/futures signing, simulation login/order test, Shioaji >=1.2, and Trading permission are documented prerequisites. | Requires credentials/account eligibility; not tested. |
| Simulation | `Shioaji(simulation=True)` exposes place/update/cancel/status/list and selected accounting APIs. Simulation does not support emerging-stock or odd-lot orders. | Officially documented, but requires credentials and is not an offline sandbox. |
| Login/session lifecycle | API-key/secret login, accounts, trade subscription, usage/connection limits, and logout are documented. | Basic lifecycle documented; token expiry and production session SLA are unknown. |
| Certificate | Production order placement requires CA activation; simulation may skip it. CA expiry query is documented. | Requires credential/certificate lifecycle decision and eligible account. |
| Submit/cancel/replace/query | `place_order`, `cancel_order`, `update_order`, `update_status`, and `list_trades` are documented. | Capability documented; race and failure semantics still require controlled verification. |
| Callbacks/fills/status/sequence | Order/deal callback payloads include `event_id` (1.7.6), SDK order `id`, `seqno`, `ordno`, deal `trade_id`, `exchange_seq`, status/deal quantities, and timestamps. Official docs warn a deal can arrive before its order event. | Sufficient to design mapping; uniqueness scope, rollover, and ordering guarantees remain unknown. |
| Client order ID/idempotency | `custom_field` is alphanumeric and at most six characters; SDK/server identities are shown. No official source found that guarantees a caller-supplied globally stable client order ID or exactly-once submit. | **Unknown**; never treat `custom_field` as an idempotency guarantee. |
| Lost response/reconciliation | `update_status` refreshes backend state; 1.7.6 adds built-in trade updates, `event_id`, and trade-cache health with SequenceGap/PendingReport/UntrackableEventId/ProjectionFailed reasons. | Reconcile-first design is supported; authoritative negative lookup and safe-resubmit semantics are unknown. |
| Reconnect | Quote transport documents reconnect events; release notes mention reconnect recovery. Trade-cache health documents reconciliation after missed reports. | End-to-end order-report reconnect guarantees are unknown; require full reconciliation. |
| Rate limits | Official limits: orders 250 calls/10s, accounting 25/5s, selected market data 50/10s, 200 subscriptions, five connections/person, 1000 logins/day; traffic tiers reset at 08:00. | Documented for 1.7.6 audit date; adapter must keep a stricter local budget. |
| Odd lot/common lot | SDK supports Common/Fixing/Odd/IntradayOdd. Simulation explicitly rejects odd lots. TWSE rules differ by session and restrict odd lots to limit/day semantics. | Production capability advertised; account/session-specific validation is required. |
| Cash long-only | Stock orders expose Cash plus margin/short/SBL conditions. | Island Quant must explicitly permit only Cash and sell-owned quantity; SDK does not make the whole account long-only. |
| T+2/buying power | Shioaji exposes settlement T/T+1/T+2, account balance, positions, and electronic trading available/used/limit. | Fields exist; their timing, availability, and exact safe buying-power formula require account verification and operator policy. |
| Price limits/suspension/hours | Contract/market signals expose reference/limits and suspension signals; TWSE officially documents sessions, suspension, odd-lot mechanics, and typical 10% limits with exceptions. | Requires licensed authoritative real-time tradability and exception-aware policy; static 10% logic is unsafe. |
| Error codes | Callback docs define `op_code == "00"` as success and otherwise expose `op_msg`; statuses include status codes/messages. | No comprehensive, versioned official error-code taxonomy was found: **unknown**. |
| Versioning/deprecation | Official release notes and upgrade guides exist; 1.7.6 is two days old at audit time. | Exact version pin and re-audit are mandatory; compatibility policy/deprecation notice period is unknown. |
| Market-data/trading-data rights | Shioaji documents API traffic limits, not a license grant for storage, non-display use, derived data, or redistribution. TWSE requires agreements/fees for covered uses. | Requires licensed data and legal/contract review. |

## Official source register

All URLs were retrieved 2026-09-24:

- Package metadata: <https://pypi.org/pypi/shioaji/json>
- Release notes: <https://sinotrade.github.io/release/>
- Installation: <https://sinotrade.github.io/>
- API signing/test: <https://sinotrade.github.io/tutor/prepare/terms/>
- Token/certificate: <https://sinotrade.github.io/tutor/prepare/token/>
- Login/logout: <https://sinotrade.github.io/tutor/login/>
- Simulation: <https://sinotrade.github.io/tutor/simulation/>
- Stock orders: <https://sinotrade.github.io/tutor/order/Stock/>
- Intraday odd lot: <https://sinotrade.github.io/tutor/order/IntradayOdd/>
- Order/deal callbacks: <https://sinotrade.github.io/tutor/callback/orderdeal_event/>
- Status/reconciliation/cache health: <https://sinotrade.github.io/tutor/order/UpdateStatus/>
- Usage/rate limits: <https://sinotrade.github.io/tutor/limit/>
- Account balance: <https://sinotrade.github.io/tutor/accounting/account_balance/>
- Trading limits: <https://sinotrade.github.io/zh/tutor/accounting/trading_limits/>
- Positions: <https://sinotrade.github.io/tutor/accounting/position/>
- Settlements: <https://sinotrade.github.io/tutor/accounting/settlements/>
- TWSE mechanism/rules: <https://www.twse.com.tw/en/products/system/trading.html>

## Explicit unknowns

Stable caller-controlled idempotency identity; exactly-once behavior; negative order lookup
authority; event and identifier uniqueness scope across reconnect/day/account; complete error-code
mapping; production cancel/replace/fill race semantics; session/token expiry; maintenance windows and
SLA; retained query horizon; certificate rotation without interruption; data retention and derived
use rights. These must not be guessed.
