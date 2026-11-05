# Decisions

## Agent architecture options

| Approach | Pros | Cons |
| --- | --- | --- |
| **Fixed workflow of LLM calls** | Predictable execution, straightforward debugging, easy to evaluate each stage, relatively controlled cost | Cannot adapt its investigation when evidence is missing or contradictory; handling new situations may require more explicit branches |
| **One agent with tools** | Can inspect relevant documents, choose further sources and investigate inconsistencies; keeps coordination simple | Can miss evidence or make unnecessary calls; needs bounded execution, clear tools and checks on its conclusions |
| **Multiple specialised agents** | Separates investigation, writing and review; independent review may catch mistakes; some work can run concurrently | More calls, cost and coordination; agents can propagate shared mistakes; disagreements need resolution; harder to trace and evaluate |

## Future improvement: explicit report outcomes

A future version could assign an explicit status to each generation result to help reviewers prioritise their work:

| Outcome | Meaning |
| --- | --- |
| Validated draft | Required checks pass with no identified outstanding review items. Human review is still required. |
| Draft requiring review | A draft was generated, but marked items such as fees or tax information require human completion or confirmation. |
| Blocked | Missing or conflicting evidence prevents a supported report; return an explanation of what needs resolving. |

These statuses are not required for the initial implementation. For now, clearly flag outstanding information and report validation failures. Introducing formal statuses later would require explicit rules distinguishing review items from blocking issues.
