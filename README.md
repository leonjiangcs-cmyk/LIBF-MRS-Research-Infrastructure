# LIBF-MRS-Research-Infrastructure

Public research infrastructure for the LIBF Market Risk State (MRS) development workflow.

## Scope

This repository is limited to reusable research infrastructure, including:

- data-source connectivity probes
- historical-data download utilities
- point-in-time handling utilities
- universe and coverage QA code
- generic Data Gate checks
- reproducible test harnesses and GitHub Actions workflows

## Data and research-intelligence boundary

Do **not** commit any of the following to this public repository:

- historical stock datasets or large CSV/GZIP research data
- Google Drive research snapshots or manifests containing private research data
- factor hypotheses, formulas, parameter choices, thresholds, or Gate standards
- factor validation results, failed-factor diagnostics, candidate-factor libraries, or ranking outputs
- production MRS factors, Exposure Gate logic, production signals, or final model composition
- any 2024+ Final Holdout data or results

Research data remains in Google Drive. Research intelligence remains in private GitHub/Notion. Only validated production components are migrated into the existing private LIBF repository.

## Current task

Historical ST data-source remediation infrastructure. The first workflow is a pre-2024 BaoStock cloud connectivity/schema probe. It queries only a small 2023 sample and contains no MRS factor evaluation.
