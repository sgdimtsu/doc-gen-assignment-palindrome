# Architectural decisions

These decisions describe the prototype's architecture and observed trade-offs.
They do not imply that every generated report is correct.

## 1. One tool-using extraction agent within a staged pipeline

**Choice:** Use one extraction agent to investigate sources and respond to validation
feedback. Run reconciliation and report writing as separate stages controlled by code.
The reviewer is a bounded model call, not another autonomous investigator or repair agent.
Read the request and specification first, then every discovered source before validation,
rather than relying on selective retrieval to decide which files to inspect.

**Alternatives:** A fixed sequence of LLM calls, multiple specialised agents with a
coordinator, or one unrestricted agent controlling the entire report process. For source
coverage, an alternative was selective retrieval based on an inventory and relevance
judgments rather than reading every file.

**Why this choice:** Investigation benefits from adaptive reading and rereading, but
not every stage needs autonomy. One agent keeps coordination simple while separate
stages make evidence capture, factual resolution and prose generation independently
inspectable. Reading every source avoids making an early retrieval decision that could
exclude an unexpected instruction or conflicting observation. The single-agent choice
avoided multi-agent coordination; it does not establish that the resulting implementation
is the smallest possible solution.

**Trade-off and evidence:** Agent behaviour and extraction completeness vary between
runs. Reading every source before validation improves coverage opportunities but adds
context, cost and latency without guaranteeing understanding. Separate stages also
create handoffs where information can be lost, and contracts and the repair loop add
their own complexity. Additional agents should be justified
by recurring, measured failures rather than added solely to separate responsibilities.

## 2. Structured, source-linked contracts between stages

**Choice:** Pass typed observations with source evidence from extraction to
reconciliation, then pass a reconciled report plan to the writer. Preserve differing
assertions and uncertainty rather than passing one combined source-text prompt to
every writing call. Code owns record metadata and internal IDs.

**Alternatives:** Concatenate all documents into each writing prompt, pass free-text
summaries between stages, or construct report prose directly during extraction.

**Why this choice:** Structured contracts give downstream code predictable inputs and
provide a shared factual basis across report sections. Source references and supporting
excerpts make it possible to inspect where a fact came from and locate which stage
introduced a mistake. Relevant evidence that does not fit a type can remain unclassified.

**Trade-off and evidence:** A schema imposes classification work and can introduce null
fields, repeated assertions and representation gaps. A contribution was sometimes
classified as a valuation, and grouped scope references sometimes lost their intended
meaning. Document-level references keep provenance simple but are less precise than
paragraph or table coordinates. Schema validity establishes shape, not factual fidelity;
contracts need evaluation against original sources.

## 3. Deterministic factual processing with constrained LLM interpretation and writing

**Choice:** Code imports supported structured facts and applies explicit reconciliation
rules for account matching, authority, deduplication and dated valuation selection.
The LLM interprets prose and images during extraction. The writer receives only the
reconciled report plan, with client-specification requirements and preserved request
instructions; it cannot independently return to raw sources to choose facts.

**Alternatives:** Ask the LLM to extract and reconcile everything, let each writing call
interpret raw evidence independently, or implement deterministic parsing and fixed
report templates for every source and section.

**Why this choice:** Supplied JSON values should not be recreated by a model. Repeatable
code is appropriate for exact properties and explicit resolution rules, while language
models remain useful for varied document wording and readable prose. Reconciliation
forms the factual boundary for writing. Code also renders tables, mandatory wording
and review notices and checks numerical preservation.

The authority policy is by fact type, not a global ranking of documents: account records
establish identity and ownership; the request establishes intended report scope; meeting
notes provide decisions and context. Valuation selection considers explicit observation
dates and retains precision, so a later approximate reading must not become an exact
figure. Ambiguous matches and unresolved conflicts remain visible rather than being
resolved through writer prose.

**Trade-off and evidence:** Deterministic mappings and rules support known structures
and cases, not arbitrary new meanings. They still depend on accurate unstructured
extraction. Grouped-scope handling restored both ISAs in the medium report, but a later
live valuation remained unmatched after extraction incorrectly marked its reference
ambiguous. Writer constraints can also produce repetition and mechanical fallback
text. Unsupported cases must remain visible; more rules should follow evidence of a
general problem rather than client-specific patches.

## 4. Advisory validation within the extraction agent's repair loop

**Choice:** Expose validation as a tool the extraction agent can call after reading
all sources. Combine deterministic checks with an independent LLM evidence review,
return findings to the extractor, and bound repair attempts. Code requests a final
advisory review if the final candidate changed or was never reviewed. Unresolved
findings remain visible without requiring a pass before a usable draft proceeds.

**Alternatives:** Block generation until validation passes, have code control the
validation-and-retry cycle outside the agent, use separate reviewer and repair
agents, or defer all checking until after report generation.

**Why this choice:** Feedback can improve extraction while the agent still has the
source context and can reread evidence. Keeping repair in the existing agent avoids
another autonomous role and its coordination overhead. Advisory continuation also
accommodates legitimate gaps, such as charges requiring later confirmation, without
asking the model to invent an answer just to pass validation. The final review
addresses changes made after earlier feedback without starting another repair loop.

**Trade-off and evidence:** Reviews add cost and latency; repairs can miss findings
or introduce new errors. Some completed runs still contained incorrect account links
or omitted valuation evidence, and reviewers sometimes produced noisy findings.
A review pass therefore does not establish factual correctness, while a generated
draft is not approval of advice. The assignment-required report evaluation assesses
these outcomes separately. Missing charges and an unresolved account identity are not
equivalent risks, even though both can currently allow draft generation. Production would
need explicit release criteria that distinguish completion gaps from material unresolved
scope, identity or financial instructions, plus evidence that the repair loop improves
quality enough to justify its cost.
