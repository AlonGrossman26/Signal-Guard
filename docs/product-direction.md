# Product direction — where SignalGuard goes after v1

**Status: a proposal, not an authorisation.** `CLAUDE.md` §13 still rules futures, MetaTrader and
multiple brokers out of scope, and that line is unchanged. Phase 10 onward needs an explicit
decision from the human before any of it is built. Nothing here contradicts v1; it describes what
v1 could become.

Written 2026-08-02, from the research in the session that closed Phase 8.

---

## 1. The problem worth solving

SignalGuard v1 solves a real problem — "stop me placing a trade that breaks my own rules" — in a
market that already has a dozen products. TradersPost alone has 40,000+ traders and a five-year head
start. Competing on *webhook-to-broker* means losing on broker count, asset classes and latency, all
at once.

But the research surfaced an adjacent problem that is **larger, more expensive, and much better
matched to what this codebase already is.**

Proprietary trading firms sell evaluations: pay a fee, trade to a profit target without breaking
their risk rules, get funded. The industry-wide failure rate is **80–95%** — one 2026 dataset of
over 300,000 accounts puts it at 93%. The cause is the part that matters:

> **71–78.7% of all failures come from breaching the daily drawdown.** Not strategy. Not max
> drawdown. The daily limit.
>
> And more precisely: *"Most failed evaluations come from breaching a **measurement mechanic** —
> equity vs balance, trailing vs static drawdown, server-time resets — rather than from a genuinely
> bad trade."*

The money is already moving. Evaluations cost **$15–$700+**, the average trader needs **2–4
attempts**, and a reset costs 50–100% of the original fee. Two failures at a larger account size is
**$600–900 spent before a single funded day**. Failures cluster in two zones: the first 7–30 days,
and *within 1–2% of the profit target*, where people oversize and breach on the finish line.

So: a large population, paying repeatedly, failing at something that is **not a trading problem**.
It is a rule-enforcement and measurement problem. That is the thing this codebase is.

---

## 2. Why this project specifically

Most of the hard parts are already built, tested, and were hard for reasons that turn out to matter
here.

| Prop-firm rule | Status in this codebase |
|---|---|
| Daily loss limit, resetting at the firm's server time | **Built.** Rule 8, with per-user `daily_reset_time` + `timezone`, DST-correct session boundaries, and a `tripped_today` block that persists after equity recovers |
| **Equity vs balance** measurement | **Built.** `total_equity` (mark-to-market, includes unrealized) and `free_balance` are separate fields — Q3 forced us to name them properly, and that exact distinction is what traders breach on |
| Flatten and block on breach | **Built.** Kill switch: cancel → close → `LOCKED` → notify, with the reconciler enforcing "locked means flat" continuously rather than once |
| Position and exposure caps | **Built.** Rules 9 and 10 |
| Proof of what your limits were when you were stopped | **Built.** Append-only decisions with reason codes and a config snapshot — a later settings edit cannot rewrite the record |
| Max drawdown — static, trailing, or end-of-day trailing | **Missing.** New rule, same shape as rule 8 |
| Consistency rule (one day ≤30–50% of total profit) | **Missing.** But `trades` already stores realized PnL with timestamps |
| News blackout windows | **Missing.** A time-window gate; cheap |

Roughly **60% of a prop-rule guard already exists**, and the parts that exist are the parts that are
annoying to get right.

### What the competition can't easily copy

The existing tools are MetaTrader Expert Advisors — KT Equity Protector, Prop Machine, and a long
tail of "pass your challenge on autopilot" bots. Their weaknesses are structural, not incidental:

* **They run inside the trading terminal.** If MT5 or the VPS dies, the protection dies with it.
  SignalGuard is a separate service with its own reconciliation loop, so it keeps enforcing when the
  trading platform is gone — which is exactly when you need it.
* **They keep no audit trail.** When a firm says you breached, you have no record of what your own
  limits were at that moment.
* **`risk/` is pure — zero I/O, time passed in.** This is the one place the architecture is a
  genuine competitive advantage rather than merely good engineering: it means a firm's exact rule
  set can be replayed over historical equity in milliseconds. A competitor whose risk logic is
  tangled with execution and clock reads cannot cheaply do that. See Phase 11.

### The positioning, in one line

**We do not help you pass. We stop you failing on a technicality, and we can prove what happened.**

That is honest, it is different from everything else in the category, and it is the same thing this
product already does — refuse reliably, and keep the receipt.

---

## 3. The plan

Ordered so that **the cheap, reversible work comes first and the scope-breaking work comes last,**
after demand has been tested. Each phase is independently useful, so stopping after any of them
leaves something worth having.

| Phase | Deliverable | Verifiable by | Needs new scope? |
|---|---|---|---|
| **9 — Prove what exists** | No new features. Close V-2 and V-1, then run it for a week against testnet with a live strategy | A real entry + stop placed and reconciled on Binance testnet; a real trade recorded; the equity curve populating | No |
| **10 — Prop rule model** | `max_drawdown` (static / trailing / EOD-trailing), consistency rule, news blackout, plus a `prop_profiles` table modelling one firm's rule set | Table-driven tests per rule with the boundary exactly at the threshold, per §13 | No — pure `risk/` work |
| **11 — Breach simulator** | Replay a firm's rule set over an equity history: *"with these rules, this account would have breached on day 6, here is the decision that would have stopped it"* | A known-breaching history produces the breach at the right bar; a clean one produces none | No — zero I/O |
| **12 — Shadow guard** | Read-only monitoring + flatten-and-block on a real prop account. **No order routing.** | On a live evaluation account: approaching the daily limit triggers flatten + lock + notify, before the firm's own system fires | **Yes** — one new broker |
| **13 — Full routing** | Signals routed through to that broker, as v1 does for Binance | Approved alert places an entry and its stop | **Yes** |

### Why this order

**Phase 9 is a gate, not a formality.** Every claim in this document rests on a system that has
never placed a live order. Building Phase 10 on an unproven Phase 8 would be building on sand.

**Phase 11 before Phase 12 is the important call.** The simulator needs **no new broker adapter at
all** — it replays history through the existing pure engine. That makes it simultaneously the
cheapest thing to build, the strongest differentiator, and the best demand test available: ship it
free, and if prop traders will not even use a free tool that tells them why they failed, that is the
answer before spending a year on adapters.

**Phase 12 is smaller than it looks.** You do not have to execute through us to guard an account.
Enforcement is "flatten and block", which needs only `get_account_state`, `cancel_all_orders` and
`close_all_positions` — **all three already exist on the `BrokerAdapter` interface**. A read-only
guard is a far smaller adapter surface than full order routing, and it is the whole product for this
audience.

### What carries over unchanged

The risk engine, the kill switch, the reconciler, the audit trail, the notifier, the dashboard, the
migration history and the entire test suite. Phase 10 adds rules to an existing chain; Phase 12 adds
one implementation of an interface that already has one. Nothing here is a rewrite.

---

## 4. The decision the human has to make

**Phases 9–11 need no scope change and can start immediately.** They are pure additions to `risk/`
plus verification of what exists.

**Phases 12–13 require opening `CLAUDE.md` §13**, which currently rules out futures, MetaTrader and
multiple brokers. That line stands until it is explicitly changed. Two questions:

1. **Does v2 scope open to a second broker?** Prop accounts live on futures platforms (Tradovate,
   Rithmic) or MT5. Without one of them, Phases 12–13 cannot exist.
2. **Which one first?** Futures (Tradovate/Rithmic) reaches the largest prop population; MT5 reaches
   the forex prop market and has the most existing competition.

Nothing in this document should be built past Phase 11 until those are answered.

---

## 5. Risks, stated plainly

**Rule drift is permanent maintenance, not a one-off build.** Firms change rules and each measures
differently. Getting one wrong means telling a user they are safe when they are not — and for a
product whose entire premise is trustworthy refusal, that failure mode is worse for us than it would
be for a competitor. Mitigation: every rule set is versioned and snapshotted into decisions exactly
as `risk_profiles` already is, so "which rules was I under?" stays answerable, and users are shown
the rule set's effective date.

**The neighbourhood has a lot of grift.** "Pass your challenge on autopilot" is the dominant claim
in this space and it is mostly false. The pitch must stay "we enforce your limits and prove it".
Drifting toward the other message would cost the only thing that makes this product credible.

**Prop firm terms of service.** Some firms restrict third-party automation. A risk *limiter* is not
a trading bot, but this needs checking per firm before marketing to their traders — and it is
cheaper to check now than after Phase 12.

**Demand is assumed, not proven.** Everything above rests on research, not on a single conversation
with a prop trader. Phase 11 exists to convert that assumption into evidence before the expensive
phases begin. If it does not, stop — and the work still leaves v1 better than it found it.

---

## Sources

* [Prop firm statistics 2026 — pass rates, payouts, industry data](https://thepropfirmguide.com/prop-firm-statistics/)
* [Prop firm evaluation pass rates: statistics and reality check](https://damnpropfirms.com/trading-guides/prop-firm-evaluation-pass-rates-statistics-reality-check/)
* [Prop firm drawdown rules explained: daily vs max vs trailing](https://the5ers.com/prop-firm-drawdown-rules-explained-daily-max-and-trailing-limits-in-2026/)
* [How much does a prop firm evaluation cost in 2026](https://alphacapitalgroup.uk/posts/how-much-does-a-prop-firm-evaluation-cost-2026)
* [The real cost of prop firm challenges: hidden fees](https://www.propscorer.com/blog/prop-firm-hidden-costs-fees-guide)
* [Prop firm rules explained 2026](https://velotrade.com/blog/prop-firm-rules-explained)
* [Daily drawdown limit EAs — the existing competition](https://www.keenbase-trading.com/best-daily-drawdown-limit-ea/)
* [TradersPost 2026 review — 40,000+ traders, category leader](https://lunefi.com/blog/traderspost-2026-full-review-no-code-trading-automation)
