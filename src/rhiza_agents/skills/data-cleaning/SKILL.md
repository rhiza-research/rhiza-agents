---
name: data-cleaning
description: Techniques for cleaning, validating, and transforming tabular data.
metadata:
  author: rhiza-research
  version: "1.0"
---

# Data Cleaning

You are now operating in data cleaning mode. Follow these systematic steps to clean and validate tabular data.

## Process

1. **Inspect the data** — Look at the first few rows, check column types, identify missing values, and note any obvious issues.

2. **Handle missing values** — Decide per-column: drop rows, fill with mean/median/mode, forward-fill, or flag as unknown. Document your choice and reasoning.

3. **Fix data types** — Convert columns to appropriate types (dates, numbers, categories). Flag any values that fail conversion.

4. **Remove duplicates** — Check for exact duplicates and near-duplicates. When removing, keep the most recent or most complete record.

5. **Standardize formats** — Normalize strings (trim whitespace, consistent casing), standardize date formats, normalize units of measurement.

6. **Validate ranges** — Check numeric columns for outliers or impossible values. Flag but don't automatically remove — ask for guidance on domain-specific thresholds.

7. **Verify referential integrity** — If multiple tables, check that foreign keys match. Report orphaned records.

8. **Document changes** — Produce a summary of all transformations applied, rows affected, and any data quality issues that remain.

## Guidelines

- Always work on a copy, never modify the original data in place
- Show before/after counts at each step
- When uncertain about a cleaning decision, ask rather than guess
- Prefer reversible transformations over destructive ones
