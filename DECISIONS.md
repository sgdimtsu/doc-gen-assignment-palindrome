# Decisions

## Agent architecture options

| Approach | Pros | Cons |
| --- | --- | --- |
| **Fixed workflow of LLM calls** | Predictable execution, straightforward debugging, easy to evaluate each stage, relatively controlled cost | Cannot adapt its investigation when evidence is missing or contradictory; handling new situations may require more explicit branches |
| **One agent with tools** | Can inspect relevant documents, choose further sources and investigate inconsistencies; keeps coordination simple | Can miss evidence or make unnecessary calls; needs bounded execution, clear tools and checks on its conclusions |
| **Multiple specialised agents** | Separates investigation, writing and review; independent review may catch mistakes; some work can run concurrently | More calls, cost and coordination; agents can propagate shared mistakes; disagreements need resolution; harder to trace and evaluate |

**Choice:** start with one extraction agent with tools. This keeps ownership clear and avoids multi-agent coordination before evaluation demonstrates a need for it. The trade-off is that extraction quality depends on one agent, so coverage and factual fidelity still need checking.

## Decision: separate extraction, reconciliation and writing

Use separate stages for capturing source observations, resolving those observations into supported findings, and expressing findings as report prose. The extraction agent preserves evidence rather than selecting authoritative facts; the writer must not resolve conflicts through prose.

This separation makes errors easier to locate and allows each stage to be evaluated and changed independently. The trade-off is additional handover structures and validation: information can be lost or distorted between stages, so their contracts must be explicit. This decision does not prescribe the reconciliation implementation, which will be documented separately when finalised.

## Decision: code owns metadata and completion checks

Code assigns internal source, observation and issue IDs, records filenames and formats, and tracks tool execution and reading coverage. The agent references supplied source metadata and copies existing account IDs only when they appear in evidence. Code validates structure and references and determines whether extraction completed.

This keeps exact metadata consistent and prevents an agent's claim of completion from overriding failed reads or invalid output. The trade-off is additional runtime bookkeeping and validation logic. These checks establish process completion, not factual accuracy or understanding; extraction fidelity still requires evaluation.

## Decision: how to determine source authority

Start with deterministic reconciliation applying explicit source-authority rules by fact type, rather than ranking whole documents globally. The extraction agent preserves source observations and relevant guidance without choosing which conflicting assertion wins. Reconciliation code applies the agreed rules; cases those rules cannot resolve remain explicitly uncertain.

| Approach | How authority is decided | Contradictory information | Missing information | Ambiguous information | Main trade-off |
| --- | --- | --- | --- | --- | --- |
| **Fixed source hierarchy** | Rank sources globally, e.g. request → database → meeting notes | Higher-ranked source wins | Fall back to the next source; flag if absent everywhere | Ranking alone cannot clarify meaning | Simple and predictable, but one source is rarely best for every fact |
| **Deterministic rules by fact type** | Code defines authority separately for ownership, scope, valuations, etc. | Explicit rules resolve known cases; flag others | Required-field checks identify gaps | Flag cases outside the rules | Consistent and testable, but rules can become complex and brittle |
| **Shared policy interpreted by one agent** | Agent applies written guidance by fact type, considering source purpose, dates and context | Compare evidence; select a supported interpretation and explain why. Flag conflicts the policy cannot resolve | Inspect other available sources. If still missing, record the gap and its effect on the report; never invent a replacement | Inspect surrounding context and related evidence. Preserve qualifications or flag uncertainty when meaning remains unclear | Flexible across varied documents, but decisions can vary between runs and require validation; investigation adds cost and latency |


## Future improvement: explicit report outcomes

A future version could assign an explicit status to each generation result to help reviewers prioritise their work:

| Outcome | Meaning |
| --- | --- |
| Validated draft | Required checks pass with no identified outstanding review items. Human review is still required. |
| Draft requiring review | A draft was generated, but marked items such as fees or tax information require human completion or confirmation. |
| Blocked | Missing or conflicting evidence prevents a supported report; return an explanation of what needs resolving. |

These statuses are not required for the initial implementation. For now, clearly flag outstanding information and report validation failures. Introducing formal statuses later would require explicit rules distinguishing review items from blocking issues.
