# Decisions

Defaults chosen while building unattended. Change any of them freely.

| Decision | Choice | Why |
|---|---|---|
| Name | CostLens | Working name from the project description |
| Region | `ap-south-1` (Terraform variable `aws_region`) | Closest to the user; nothing hard-codes it |
| Python | Code targets 3.12 (Lambda runtime), developed on 3.11 | The dev machine's 3.12 is an MSYS build that cannot install wheels; 3.11 is the Windows build |
| Money | Integer cents everywhere; baselines are `Fraction`; rounding uses largest remainder | "Contributions sum to the total to the cent" must hold exactly, not approximately |
| Warehouse | Normalised rows as date-partitioned Parquet in S3 (overwrite per day), DuckDB to query | Re-running a day replaces it, so loads are idempotent by construction; no database server |
| App state | SQLite (alerts, snooze, findings, explanations) | No RDS cost or server; swap for Postgres later |
| Cost Explorer | Used for daily totals and reconciliation only | CE cannot return resource IDs and allows at most 2 group-bys; resource detail comes from CUR |
| Attribution baseline | Per-key same-weekday median; baseline total = sum of per-key baselines | Makes the split additive. Anomaly detection runs on the total series separately |
| Dashboard | Thin (trend, waste, explanation history) | Lowest interview value of the nine features |
| LLM | Behind a `Writer` interface; fake writer is the default in tests; model ID comes from config | Swappable, testable offline |
| Git | Local repository only; nothing pushed | Publishing is the owner's decision |
