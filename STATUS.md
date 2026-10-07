# Status

Written at the end of an unattended build session. Read this before trusting anything.

## Done and tested (214 tests pass: `pytest -o addopts=""`)

| Area | What exists | How it was verified |
|---|---|---|
| Attribution engine | Additive contributions per dimension, volume/rate split, drill-down, baseline medians | Hypothesis property tests on random inputs (sums to the cent, `before + change == after`), hand-made cases |
| Anomaly detection | Same-weekday median baseline, robust z-score, dual threshold, new/short-history services, fee/tax/credit exclusion | Unit tests plus 600 ordinary days with no false alarm |
| CloudTrail matching | Exact vs time-only confidence, decoy events, ARN/name matching | Unit tests on LookupEvents-shaped dicts |
| Waste rules | Six rules, ignore tags with expiry, batch detection, CUR-priced savings | Pure-function tests and moto (EC2, EBS, EIP, NAT, ELBv2, Auto Scaling, CloudWatch) |
| Explanation layer | Fact sheet, validator, retry, template fallback, token budget, cache by fact-sheet hash, Claude writer | Fake writers; Claude writer tested against a fake client (request shape, refusal, truncation, API errors) |
| Slack | Digest text, Block Kit, signature verification (fresh and valid only), Acknowledge/Snooze | Unit tests; signed, stale, tampered and unsigned requests |
| Data layer | CUR row mapping, Cost Explorer totals, idempotent day loads (DuckDB and S3 Parquet) | moto S3, a CUR-shaped Parquet file with a MAP tags column |
| Handlers | Ingest (loads 4 days for CUR revisions), digest, Slack-button Lambda, S3-synced state | End to end against moto S3, Secrets Manager and stubs |
| API | FastAPI with token-guarded writes and signed Slack endpoint | TestClient |
| Evaluation | Synthetic incidents in two difficulty tiers, pairs, false alarms | `eval/results.md` (full run: 10 seeds) |
| Terraform | S3, IAM read-only role, 3 Lambdas, schedules, secrets, budget, optional CUR export | `terraform fmt -check` and `terraform validate` against the real AWS provider (version in `terraform/.terraform.lock.hcl`), Terraform 1.9.8 |
| Lambda package | `scripts/build_lambda.py`: 20.3 MB zipped, 62.6 MB unzipped, Linux duckdb wheel inside | Built and inspected |
| Dashboard | Trend, day detail, waste list, explanation history, table view, dark mode | `npm run build`; viewed in a browser against the synthetic demo data |

## Not verified (assumptions to check on first contact with a real account)

1. **CUR 2.0 column names and S3 layout.** Confirmed from AWS docs: the table name
   `COST_AND_USAGE_REPORT`, resource-level rows via `line_item_resource_id`, nested tags. Taken from
   the legacy schema and **not** confirmed: `line_item_usage_start_date`,
   `line_item_unblended_cost`, `line_item_usage_amount`, `line_item_product_code`,
   `product_region_code`, `line_item_usage_type`, `line_item_line_item_type`, the tags column name
   and its key format (`user_team`?), and the `BILLING_PERIOD=YYYY-MM` folder layout. All of it is
   isolated in `curmap.py` (names) and `handlers.list_cur_objects` (layout). A missing column fails
   loudly with the column names.
2. **Cost Explorer response shape** is from the API reference, not a live call.
3. **Terraform has been validated, not planned or applied.** `plan` needs credentials. The
   `aws_bcmdataexports_export` resource (off by default) passes schema validation but its SQL
   column list is untested; the bucket policy for billing delivery follows the docs but is untested.
4. **The Claude writer has never made a real API call.** The request shape follows the Anthropic
   SDK documentation. The model comes from `COSTLENS_MODEL` (default `claude-opus-5-5`). I did not
   enable the server-side refusal-fallback beta; instead a refusal or any API error falls back to
   the template writer, which is the tested path.
5. **Savings estimates** use rough list-price constants when the CUR has no line for a resource.
   They are labelled "estimate" and are unverified against the AWS Pricing API.
6. **State in S3 is last-write-wins** (SQLite file synced per run). Fine for one daily digest plus
   occasional button clicks; use DynamoDB or Postgres for more.
7. **Slack**: message formatting is tested as data; nothing has been posted to a real workspace.
8. **Python version**: developed on 3.11 (this machine's 3.12 is an MSYS build that cannot install
   wheels). Lambda runs 3.12; the package zip contains the cp312 Linux duckdb build. CI runs both.
9. **The evaluation is synthetic** (see the README's "How to read this honestly"). There are no
   real-incident results yet.
10. No Adtributor citation is made anywhere; the dimension-selection idea is described on its own
    terms (fewest values explaining most of the change, with a distribution-shift score).

## Things only you can do

- [ ] Create or choose the AWS account; set the budget alarm; enable Cost Explorer.
- [ ] Create the CUR 2.0 export (or `create_cur_export = true`) and wait for the first file.
- [ ] Check `curmap.py` against the first real file (column names, tags key format) and adjust.
- [ ] `terraform plan`, read it, `terraform apply` (I never ran apply and never will without you).
- [ ] Create the Slack app; store its bot token and signing secret in the Secrets Manager secret.
- [ ] Optional: store an LLM API key in the `llm` secret and set `enable_ai = true`.
- [ ] Run a few real sandbox incidents (small instance, a few hours, tear down) and add the results.
- [ ] Choose a license.

## Decisions made on your behalf

See [`DECISIONS.md`](DECISIONS.md). Notable ones: SQLite instead of RDS, DuckDB and Parquet instead
of a database server, Cost Explorer for reconciliation only (it cannot return resource IDs), region
`ap-south-1` as a variable, a thin dashboard.

## Housekeeping

- Git history is local-first and pushed to `kuheli7/costLens` (private) at your request. Every
  commit has only your name and no co-author or tool-attribution lines.
- No AWS call of any kind was made against your account: no Cost Explorer request, no resource
  created, no `terraform apply`. The only AWS commands run were `aws sts get-caller-identity` and
  `aws configure list-profiles`, to see what existed.
- A downloaded Terraform binary and its provider cache were kept in a scratch directory outside
  the repository.
