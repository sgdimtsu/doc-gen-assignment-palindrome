# Pipeline requirements and success criteria

## Purpose

Turn a client's structured and unstructured source material into an accurate draft investment advice report for human review. Express the adviser's supported instructions; do not invent advice, missing facts or financial figures.

The same implementation must work across the supplied clients and unseen clients with similar themes. Architecture choices are provisional until evaluated.

## Functional requirements

Labels distinguish the basis of each requirement:

- **Assignment-mandated**: explicitly required by the assignment or report specification.
- **Evidence-derived**: needed to handle the supplied client data and notes correctly.
- **Proposed**: an engineering safeguard or implementation policy to evaluate.

A requirement can have multiple labels where its success criterion combines a required behaviour with a proposed mechanism.

| ID | Requirement | Basis | Success criterion |
| --- | --- | --- | --- |
| R1 | Read the relevant evidence | Evidence-derived; Proposed | Support JSON, Word documents and statement images. Discover optional files without requiring an identical file inventory for every client. Record unreadable or unsupported evidence explicitly. |
| R2 | Establish report scope | Assignment-mandated; Evidence-derived | Identify accounts covered by the adviser's request. Distinguish covered accounts, funding sources, background accounts and proposed accounts. Do not turn future aspirations into current recommendations. |
| R3 | Reconcile account records | Evidence-derived | Preserve account identity, ownership and platform. Consolidate repeated joint accounts without merging distinct accounts. Distinguish missing values from zero and recognise closed accounts. |
| R4 | Reconcile evidence | Assignment-mandated; Evidence-derived; Proposed | Apply explicit source-authority rules by fact type, consider valuation dates and retain approximate wording. Flag material unresolved conflicts rather than silently choosing or manufacturing an answer. |
| R5 | Preserve provenance | Proposed | Every material reported fact and derived amount must be traceable to source evidence or a recorded calculation. Supporting evidence may be stored separately from the client-facing report. |
| R6 | Follow supported instructions | Assignment-mandated; Evidence-derived | Recommendations reflect the agreed actions and supported amounts. Separate available funds from committed funds and contingent future receipts. Do not invent allocation amounts or assume unverified allowances. |
| R7 | Follow the report specification | Assignment-mandated | Produce sections in the required order. Background remains high level, with an Account / Owner / Type / Value table. Transaction details belong in Recommendations and Tax Implications. |
| R8 | Include tax content conditionally | Assignment-mandated; Evidence-derived | Include Tax Implications only for a recommended disposal that may create a taxable capital gain. When included, describe the potential liability and annual exempt amount without inventing a tax estimate. |
| R9 | Preserve mandatory wording | Assignment-mandated | The FCA authorisation sentence and specified investment risk warning appear verbatim. Model generation cannot rewrite them. |
| R10 | Make human review actionable | Evidence-derived; Proposed | Clearly identify missing or unconfirmed CGT, platform charges and ongoing advice charges, with the relevant account or platform where applicable. Other material unknowns must also be visible. |
| R11 | Validate before delivery | Proposed | Check the report against structured findings and the specification. Clearly report critical validation failures. Retain diagnostics for investigation. |
| R12 | Run end to end | Assignment-mandated | A documented command produces the client's Markdown report under outputs/ from a clean checkout with the required credentials. Preserve the provided formatter as instructed. |

R1's explicit ingestion diagnostics and R4's formal source-authority rules are proposed mechanisms. R10 extends the manual-review guidance to other material unknowns. The prohibition on estimating CGT and platform/adviser fee rates is explicit in client 01's notes; applying it across clients is a proposed shared drafting policy.

## Evaluation and acceptance

Writing an evaluation is assignment-mandated. The measures, acceptance targets and testing methods below are proposed.

Establish expected facts and review items through manual investigation of all four clients before using generated reports as evidence of correctness. Expected case facts belong in evaluation fixtures, never in generation rules.

| Dimension | Measure | Initial acceptance target |
| --- | --- | --- |
| Scope and account accuracy | Required accounts present; inappropriate accounts absent; correct ownership and deduplication | No known critical errors across the supplied cases |
| Financial accuracy | Reported values, dates, transactions and calculations compared with evidence | No unsupported material figures; no known calculation errors; approximations remain qualified |
| Uncertainty handling | Expected missing facts and material conflicts surfaced | Every identified material issue is flagged or blocks completion appropriately |
| Section correctness | Required headings, ordering, conditional tax inclusion and table columns | All deterministic checks pass |
| Mandatory wording | Exact comparison against required text | All checks pass |
| Relevance and tone | Human review of scope, section placement, sensitivity and clarity | No material irrelevant advice or misleading wording |
| Generalisation | Rerun with changed names and amounts, reordered records, optional-file changes and missing fields | Behaviour follows the changed evidence without client-specific patches |
| Reliability | Repeat generation on representative cases and inspect failures | No observed critical regressions; report sample size rather than claim guaranteed accuracy |
| Efficiency | Record model calls, token usage, estimated cost and elapsed time per client | Measure the starter and improved pipeline; set numerical budgets after obtaining a baseline |
| Reproducibility | Document setup and exercise the generation and evaluation commands | Works from a clean checkout with documented dependencies and credentials |

Use deterministic checks for exact properties and evidence-based human review for interpretation. A model reviewer may supplement evaluation but must not be the sole judge of factual correctness. Passing supplied cases does not establish performance on the unseen set.

## Trade-off priorities (proposed)

1. Factual correctness, scope and visible uncertainty take precedence over fluent prose or speed.
2. A supported draft with explicit review gaps is preferable to a polished report with invented figures.
3. Use deterministic code for exact checks and calculations; use model reasoning where interpretation is needed.
4. Choose the simplest architecture that meets the requirements. Add agents or model calls only when evaluation demonstrates useful improvement.
5. Optimise cost and latency after establishing an acceptable correctness baseline; retain quality checks when optimising.
