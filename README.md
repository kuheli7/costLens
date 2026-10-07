# CostLens

**Your AWS bill, explained like a teammate would: with the resource, the cause and the fix.**

CostLens answers one question every morning: *why did our AWS bill change, and what should we do
about it?* It finds the service, group, resource and CloudTrail event behind a cost change,
explains it in plain English on Slack, and lists idle or oversized resources with the money they
waste. It runs read-only inside your own AWS account.

> **Status: built and tested locally, not yet run against a live AWS account.** Everything below
> that says "tested" means tested against mocked AWS (moto), recorded fixtures and synthetic
> data. [`STATUS.md`](STATUS.md) lists exactly what has and has not been verified.

## What is different from "Cost Explorer plus an LLM summary"

1. **Resource-level root cause.** It uses CUR resource IDs plus CloudTrail to say
   "`i-0abc`, an `m5.4xlarge`, launched by `alice` at 02:14, added $19.20/day", not "EC2 is up".
2. **Numbers the LLM cannot invent.** A deterministic engine computes every figure. The model only
   writes sentences, and a validator rejects any number, resource ID, time, date or name that is
   not in the fact sheet. Failing text is retried once, then replaced by template text.
3. **Measured accuracy.** An evaluation harness injects known incidents and scores whether the
   right cause is named. See [Results](#results), including what they do *not* show.

## How it works

```
 EventBridge (daily)
        |
        v
  ingest Lambda --- CUR 2.0 Parquet (S3) ---> normalised daily rows (Parquet, one file per day)
        |            Cost Explorer totals ---> reconciliation check
        v
  digest Lambda
     1. baseline      same-weekday median, robust z-score, alert only if large in % AND $
     2. attribution   exact, additive change per dimension; volume vs rate split; drill-down
     3. context       CloudTrail events matched to the cause (exact resource vs time-only)
     4. waste rules   six read-only rules with savings priced from your own bill
     5. fact sheet    every figure pre-computed and formatted
     6. writer        Claude (optional) or template -> validator -> retry once -> template
     7. Slack         Block Kit digest with Acknowledge / Snooze buttons
        |
        v
  SQLite state (alerts, findings, explanation history) --> FastAPI --> React dashboard
```

The language model enters at step 6, after every number is fixed. With AI off, the whole pipeline
runs on the template writer.

### The engine (where the interview depth is)

- **Volume vs rate.** Every cost line is `usage x unit price`. The change splits as
  `(q1 - q0) * p0` (volume) plus `(p1 - p0) * q1` (rate), so "you ran more instance-hours" and
  "the price per hour rose" are told apart. New or vanished line items are all volume.
- **Additive to the cent.** Money is integer cents. Baselines are exact `Fraction`s and rounding
  uses largest-remainder allocation, so contributions in every dimension sum to the total change
  exactly. Property tests (Hypothesis) check this on random inputs, and that printed
  `before + change == after` for every node.
- **Hierarchical drill-down.** Pick the dimension where the fewest values explain the change
  (service, region, usage type, team tag, Auto Scaling group, resource), recurse into them, and
  report a diffuse change (five new instances in one group) as the group with its top
  contributors rather than as five separate causes.
- **Data correctness.** Tax, credits and refunds are excluded from root-cause analysis and
  reported separately; monthly fee lines (support plans) are excluded from the comparison; CUR
  days younger than 72 hours are flagged provisional; Cost Explorer and CUR totals must agree
  within a tolerance or the digest says the data is incomplete; every day load replaces that day.

### Waste rules (read-only)

Idle EC2, oversized Auto Scaling group, unattached EBS volume, stopped instance with storage,
unattached Elastic IP, idle NAT gateway or load balancer. Each returns the resource, the
evidence, the monthly saving and a fix command (text only; nothing is ever executed). Savings come
from what the resource actually cost on your bill; only when the CUR has no line for it does the
finding fall back to a rough list-price estimate, and it is labelled "estimate". A
`costlens:ignore=true` tag with `costlens:ignore-until=YYYY-MM-DD` suppresses a finding; resources
younger than the lookback window are skipped; a workload busy a little every day is treated as
batch, not idle.

## Results

From `python -m costlens.evaluate` (full output: [`eval/results.md`](eval/results.md)). Each run
injects a labelled incident into a fresh 90-day synthetic account and runs the real pipeline.
Six incident kinds (Auto Scaling scale-out, a new large instance, NAT traffic spike, a rate
change, a brand-new service, a data-transfer spike), 10 seeds, incident ages of 0 and 2 days.

| Metric (120 incidents per tier) | Standard (3% noise) | Hard (8% noise, half-size incidents) |
|---|---|---|
| Correct root cause named | 120/120 | 120/120 |
| Volume vs rate named correctly | 120/120 | 120/120 |
| Cause amount within 10% of the true added cost | 120/120 | 96/120 (80%) |
| Right CloudTrail actor, or an honest "not confirmed" | 120/120 | 120/120 |
| Contributions sum to the total in every dimension | 120/120 | 120/120 |
| Invented facts caught by the validator (6 per digest) | 720/720 | 720/720 |
| Ordinary days wrongly flagged | 0/300 | 0/300 |
| Two incidents at once: both in the top three causes | 30/30 | 30/30 |
| Two incidents at once: larger one ranked first | 30/30 | 20/30 (67%) |

**How to read this honestly.** These are synthetic incidents that I designed, on a generator I
also wrote, so the engine and the data share assumptions (for example that resource IDs are
present). 100% here means the engine does what it was designed to do on clean, labelled cases;
it is **not** a claim about accuracy on a real account. Real bills have savings plans,
reservations, shared resources, tag drift and incidents that overlap in messy ways. The hard
tier is where the weak points show: the dollar amount drifts when day-to-day noise is high, and
two simultaneous incidents of similar size can swap rank. Incidents smaller than the $5/day alert
floor are silent by design and are not scored. The next step is to add real sandbox incidents
(launch a small instance, run it for a few hours, tear it down) and report those separately.

The validator checks that every number, identifier and name is *in the fact sheet*. It cannot
check that a real number is used with the right meaning ("17" quoted where the fact sheet's 17
means something else), so cause sentences also come from structured per-cause fields.

## Quick start (no AWS account needed)

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
pytest                                   # 237 tests: engine, waste rules (moto), handlers, API, Slack
python -m costlens.evaluate              # the evaluation above, about 10 seconds
python scripts/export_demo.py            # synthetic demo data for the dashboard
cd dashboard && npm install && npm run dev
```

The dashboard falls back to the bundled synthetic demo data when the API is not running.

## Deploying to your own AWS account

Untested against a live account; see [`STATUS.md`](STATUS.md) for the checklist and the
assumptions to verify on first contact (CUR column names, S3 layout).

1. Use a **sandbox account** and set a budget alarm first (`budget_alert_email` in Terraform).
2. Turn on Cost Explorer. Create a CUR 2.0 (Data Exports) export, Parquet, hourly, with resource
   IDs, delivered to the bucket under `cur/` (or set `create_cur_export = true` in Terraform).
3. `python scripts/build_lambda.py` builds `build/costlens-lambda.zip` (about 20 MB).
4. `cd terraform && terraform init && terraform plan` and read the plan, then `terraform apply`.
   Region and profile are variables (`aws_region`, `aws_profile`); nothing hard-codes an account.
5. Put the Slack token and signing secret, and optionally an LLM key, into the two Secrets Manager
   secrets Terraform created. Secrets never live in code, state or environment files.
6. Set the Slack app's interactivity URL to the `slack_interactions_url` output.

Permissions: one IAM role, **read-only** on the account (`ce:Get*`, `cloudtrail:LookupEvents`,
`cloudwatch:GetMetricData`, `Describe*` on EC2, Auto Scaling, ELB, RDS) plus read/write on the
tool's own bucket only. There is no write permission to anything else. Slack button clicks are
accepted only with a valid, fresh Slack request signature.

### What it costs to run

Estimates, not measurements; check current AWS prices before relying on them.

| Item | Rough cost |
|---|---|
| Lambda, EventBridge, CloudWatch Logs at one run a day | free tier or cents |
| S3 (CUR plus normalised Parquet) | cents |
| Cost Explorer API (about $0.01 per request, one range per day) | cents |
| Secrets Manager (two secrets, about $0.40 each per month) | about $0.80 |
| LLM (one short call per digest, cached, with a hard token budget) | cents, and $0 with AI off |

The target is under $5 a month. The first real figure will come from the bill of a sandbox account
that runs it, which is exactly the kind of number this tool is meant to report.

## Project layout

```
src/costlens/
  attribution.py   additive contributions, volume/rate split, drill-down
  baseline.py      same-weekday baseline, robust z-score, thresholds
  report.py        one verified DayReport (the source of every number)
  cloudtrail.py    event matching with confidence labels
  waste/           inventory (Describe*), CloudWatch metrics, prices, six rules
  factsheet.py     what the language model is allowed to see
  validate.py      rejects numbers/IDs/names not in the fact sheet
  explain.py       writer interface, retry, template fallback, token budget, Claude writer
  slack.py         digest, Block Kit, signature check, button handling
  state.py         SQLite: alerts, findings, explanation history (thread-safe)
  curmap.py        CUR 2.0 -> rows (the one place that knows CUR column names)
  ce.py            Cost Explorer daily totals
  store.py         idempotent daily loads: DuckDB (dev) and S3 Parquet
  handlers.py      Lambda entry points (ingest, digest, Slack buttons)
  api.py           FastAPI service for the dashboard
  synth.py         synthetic accounts with labelled incidents
  evaluate.py      the evaluation harness
terraform/         S3, IAM (read-only), Lambdas, schedules, secrets, budget (validated)
dashboard/         React + Recharts
scripts/           Lambda build, demo export
```

## Out of scope

Automatic deletion of anything (the tool suggests; a human acts), non-AWS clouds, and Reserved
Instance or Savings Plan purchase advice. Fix pull requests, deploy correlation, forecasting,
multi-account support and question answering over the cost data are not built.

## License

Not yet chosen.
