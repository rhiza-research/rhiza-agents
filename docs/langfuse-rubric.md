# Langfuse Score Configs and Evaluators

This is a setup guide for configuring score schemas and online LLM-as-judge
evaluators in the rhiza-agents Langfuse project.

## What this rubric measures

The scores below evaluate **how well a trace exposes a reproducible
workflow**, not how good the AI's answer was. They measure decomposition,
lineage, re-runnability, and expertise encoding — the properties that make
an AI-augmented system into a dependable decision system rather than an
opaque oracle.

The rubric is **deliberately architecture-agnostic**. It doesn't reference
supervisors, workers, handoffs, or any specific orchestration pattern, so
it stays valid as the agent layout evolves. The scores are about observable
trace properties: what was decomposed, what was traced, what was made
reproducible.

Setup is one-time per Langfuse instance. Reuse the same names in any future
production instance so dashboards and saved filters carry over.

---

## Score configs

Create each via **Settings → Scores → New score config** in the Langfuse UI.
These are the manual rubric the domain experts apply to traces, plus the
score configs the LLM judges below write into.

For CATEGORICAL configs, Langfuse requires an integer **value** alongside
each category label (the form will fail validation with "expected number,
received string" otherwise). The values are arbitrary but must be unique
per config; the suggested convention is to assign descending integers in
order of "best to worst" (e.g. `walked_through=2`, `partial=1`,
`short_circuited=0`) so numeric aggregations and threshold filters behave
sensibly.

### Workflow structure

#### `workflow_decomposition`

- **Data type:** CATEGORICAL
- **Categories:**
  - `walked_through` — the system broke the user's intent into explicit
    sequential steps before producing any output (e.g. clarify → identify
    → evaluate → interpret → decide)
  - `partial` — some decomposition happened but key steps were skipped or
    collapsed
  - `short_circuited` — the system jumped to an answer without exposing
    intermediate reasoning
- **Applied by:** Domain experts (manual) and the workflow decomposition
  judge below
- **Why:** The headline structural signal. A system that jumps straight
  to an answer instead of walking the user through explicit reasoning
  steps is working against the product direction regardless of whether
  the answer was correct.

#### `clarification_quality`

- **Data type:** NUMERIC, range 1–5
- **Description:** When the user's intent was vague or ambiguous, did the
  system ask the right clarifying questions before committing to a path?
  1 = ignored ambiguity and guessed, 5 = surfaced every important
  ambiguity and resolved it explicitly with the user.
- **Applied by:** Experts (manual)
- **Why:** A workflow that starts from a misunderstood intent isn't
  reproducible — the next person hitting the same vague question gets a
  different result. Clarification is the entry point to reproducibility.

#### `reasoning_exposed`

- **Data type:** NUMERIC, range 1–5
- **Description:** Were the intermediate reasoning steps (why this metric,
  why this region, what failure modes apply, what the confidence is)
  surfaced to the user, or hidden inside opaque AI reasoning?
- **Applied by:** Experts (manual)
- **Why:** A trace whose reasoning lives only inside the model's thinking
  tokens is opaque to anyone reviewing it later, even if the final answer
  is good. Hidden reasoning is not auditable reasoning.

### Lineage and reproducibility

#### `lineage_completeness`

- **Data type:** NUMERIC, range 1–5
- **Description:** For every factual claim, number, or output in the
  conversation, can a third party reading this trace identify the exact
  data source, query, and intermediate transformation that produced it?
  1 = most claims are unsourced, 5 = every claim is fully traceable.
- **Applied by:** Experts (manual) and the lineage judge below
- **Why:** Full lineage from claim back to source is the structural
  prerequisite for any of the system's outputs to be auditable.

#### `tool_grounding`

- **Data type:** BOOLEAN
- **Description:** Is every factual claim in the conversation supported
  by a tool output visible in the trace, with no fabricated data?
- **Applied by:** Experts (manual) and the grounding judge below
- **Why:** A binary structural check that catches the most common failure
  mode — the model inventing numbers or claims that didn't come from any
  tool.

#### `re_runnability`

- **Data type:** CATEGORICAL
- **Categories:**
  - `re_runnable_without_ai` — a human reading this trace could
    re-execute the same workflow without any AI in the loop and produce
    the same result
  - `re_runnable_with_ai` — re-running the trace requires the AI to
    re-make decisions but those decisions are explicit and reproducible
  - `not_re_runnable` — the trace contains hidden steps, opaque
    transformations, or AI-as-source-of-truth claims that prevent
    reproduction
- **Applied by:** Experts (manual) and the re-runnability judge below
- **Why:** Being able to re-run a workflow without the AI in the loop is
  the hardest reproducibility test — and the one that distinguishes a
  decision system from a useful but unverifiable assistant.

### Output form

#### `output_form`

- **Data type:** CATEGORICAL
- **Categories:**
  - `versioned_artifact` — the conversation produced something persistent
    that lives outside the chat (a pipeline component, a workflow
    definition, a parameterised script committed to a repo, etc.)
  - `structured_workflow` — the conversation produced a documented,
    repeatable workflow even if it isn't yet a versioned artifact
  - `one_off_answer` — the conversation produced only an answer that
    exists nowhere outside this trace
- **Applied by:** Experts (manual)
- **Why:** This is the highest-leverage signal for whether the system is
  producing durable value. A `one_off_answer` evaporates the moment the
  conversation ends; a `versioned_artifact` keeps paying back.

#### `expertise_encoding`

- **Data type:** NUMERIC, range 1–5
- **Description:** Did this conversation make tacit expertise explicit and
  reusable? E.g. did it surface heuristics, failure modes, model
  limitations, or contextual judgements that would otherwise live only in
  the expert's head? 1 = no expertise was encoded, 5 = the trace itself
  is now documentation that another expert could learn from.
- **Applied by:** Experts (manual)
- **Why:** The point of the system is not to use the AI's knowledge — it
  is to capture and replay the expert's. A trace that doesn't make any
  expert reasoning explicit has lost the value the expert brought to it.

### Human validation

#### `human_validation_support`

- **Data type:** NUMERIC, range 1–5
- **Description:** Did the trace contain explicit checkpoints where a
  human could (or did) validate the system's interpretation, choices, or
  intermediate results before the workflow committed to a path? 1 = no
  validation points, 5 = every meaningful decision was validated.
- **Applied by:** Experts (manual)
- **Why:** A workflow that runs end-to-end without any human checkpoints
  is fast but unauditable, and it makes errors compound silently across
  steps before anyone can catch them.

---

## Online LLM-as-judge evaluators

These run automatically on production traces and pre-populate the score
configs above so the experts have a starting point when they review.
Each judge is two pieces in Langfuse: an **evaluator template** (a prompt
+ score-type definition, lives at **LLM-as-a-Judge → Custom Evaluator**)
and a **running evaluator** that points at a template, configures the
filter / sample rate / variable mapping / target score config, and
actually fires on incoming data (created via **LLM-as-a-Judge → Set up
evaluator**).

For the local instance, set sample rate to 100% so every test trace gets
pre-scored. Drop to 10–25% in any future production instance once the
judge prompts are stable.

### Choosing a judge model

Default to **Sonnet** (currently `claude-sonnet-4-5`) for all four judges
until you have measured agreement with the experts. The judges only fire
once per trace (see "Filter to the root span" below), so per-call cost is
not worth optimizing before calibration. Haiku is the right *eventual*
target for `tool_grounding` because that judge is essentially a
substring/entailment check, but it tends to be unreliable on the more
nuanced rubric items (`lineage_completeness`, `re_runnability`,
`workflow_decomposition`) where it either settles into the middle of the
scale or over-calls the most permissive category.

The only objective way to pick a judge model is to score ~20
representative traces yourself, then check inter-rater agreement (Cohen's
kappa or just % agreement) between you and the judge. ≥80% agreement is
the usual bar for "good enough"; below that, upgrade the model or sharpen
the prompt.

### Targeting: observation mode + root-span filter

Langfuse's current evaluator API targets **observations**, not traces.
The legacy "Traces" target still exists but is marked deprecated and the
UI will warn you off it. To get **one judgment per conversation** under
the observation-targeted API, filter every running evaluator to the root
span of the LangGraph trace:

- `type` **any of** `SPAN`
- `name` **any of** `LangGraph`
- `environment` **none of** `langfuse-llm-as-a-judge`,
  `langfuse-prompt-experiment`, `langfuse-evaluation`, `sdk-experiment`
  (this is Langfuse's default exclusion list — keeps the judges from
  evaluating their own output)

The LangGraph root span's `input` and `output` fields contain the entire
conversation as the messages array, so a single observation has all the
context the rubric needs.

### Variable mapping

Because everything has to come from the root span's `input` or `output`,
the rubric variables don't map cleanly one-to-one. The mapping table
below is what we landed on — it's an approximation, and the judge LLMs
have to extract the relevant slice (final assistant message, tool result
content, etc.) out of the messages JSON themselves. The prompt templates
below are written to expect this and instruct the judge accordingly.

| Evaluator | Template variable | Object Field |
|---|---|---|
| `workflow_decomposition` | `user_intent` | Input |
| `workflow_decomposition` | `trace_summary` | Output |
| `lineage_completeness` | `final_response` | Output |
| `lineage_completeness` | `tool_outputs` | Input |
| `tool_grounding` | `final_response` | Output |
| `tool_grounding` | `tool_outputs` | Input |
| `re_runnability` | `user_intent` | Input |
| `re_runnability` | `trace_summary` | Output |
| `re_runnability` | `final_response` | Output |

The semantic mismatch — `tool_outputs` and `final_response` aren't
actually different observation fields — is a known limitation of running
trace-level rubrics through the observation-targeted API. If Langfuse
ever ships per-variable JSONPath extraction we should revisit this and
pull the messages array apart properly.

All four judges below use the same filter (`type=SPAN`, `name=LangGraph`,
default environment exclusions) — see "Targeting" above.

### Judge 1: Workflow decomposition

- **Writes to:** `workflow_decomposition`
- **Variables:** `user_intent` (the user's first message), `trace_summary`
  (a structured summary of the trace's tool calls and assistant turns in
  order)
- **Prompt template:**
  > You are evaluating whether an AI system structured a user's request
  > into an explicit, sequential workflow before producing output, or
  > short-circuited to an answer.
  >
  > A well-structured workflow walks the user through phases like:
  > clarifying the request → identifying relevant resources → evaluating
  > options → interpreting results → forming a strategy or decision. Each
  > phase should be visible in the trace as distinct steps with their own
  > tool calls or reasoning, not collapsed into a single answer.
  >
  > User intent:
  > {{user_intent}}
  >
  > Trace summary (in order):
  > {{trace_summary}}
  >
  > Did the system walk through explicit decomposition phases?
  > Reply with exactly one of:
  > `walked_through`, `partial`, `short_circuited`.

### Judge 2: Lineage completeness

- **Writes to:** `lineage_completeness`
- **Variables:** `final_response` (last assistant message), `tool_outputs`
  (concatenated content of all tool result observations in the trace)
- **Prompt template:**
  > You are evaluating how well an AI system's output traces back to
  > specific data sources.
  >
  > Final response:
  > {{final_response}}
  >
  > Tool outputs available during the conversation:
  > {{tool_outputs}}
  >
  > For every factual claim, number, name, or specific data point in the
  > final response, is it possible to identify which tool output produced
  > it? Score 1 (most claims are unsourced) to 5 (every claim is fully
  > traceable to a specific tool output). Reply with only the integer
  > score.

### Judge 3: Tool grounding

- **Writes to:** `tool_grounding`
- **Variables:** `final_response`, `tool_outputs`
- **Prompt template:**
  > You are checking whether an AI system fabricated any facts not
  > present in its tool outputs.
  >
  > Final response:
  > {{final_response}}
  >
  > Tool outputs the system received during this conversation:
  > {{tool_outputs}}
  >
  > Does the response contain any factual claim, number, or specific
  > data point that cannot be derived from the tool outputs? Reply with
  > exactly `true` if the response is fully grounded, or `false` if any
  > claim is fabricated or unsupported.

### Judge 4: Re-runnability

- **Writes to:** `re_runnability`
- **Variables:** `user_intent`, `trace_summary`, `final_response`
- **Prompt template:**
  > You are evaluating whether an AI conversation could be reproduced by
  > a human without re-running the AI.
  >
  > A re-runnable workflow has: explicit tool calls with explicit
  > parameters, intermediate results captured at each step, no hidden
  > transformations, and no claims that exist only because the AI
  > "decided" them.
  >
  > User intent:
  > {{user_intent}}
  >
  > Trace summary (tool calls and results in order):
  > {{trace_summary}}
  >
  > Final response:
  > {{final_response}}
  >
  > Could a human, reading this trace alone, re-execute the same
  > workflow manually (calling the same tools with the same arguments,
  > applying the same transformations) and arrive at the same result?
  > Reply with exactly one of:
  > `re_runnable_without_ai` — a human could reproduce it end-to-end
  > without any AI involvement
  > `re_runnable_with_ai` — reproducing it requires the AI to make
  > decisions, but those decisions are explicit and consistent
  > `not_re_runnable` — the trace contains hidden steps, opaque
  > transformations, or claims with no traceable origin

---

## How the team uses this in practice

1. **Engineers stand up the rubric once** in the local Langfuse instance
   using the score configs above.
2. **Engineers configure the four LLM judges** with the templates above
   and set sample rate to 100% locally.
3. **Domain experts log into Langfuse** and review traces from real or
   test conversations. Useful starting filters:
   - `workflow_decomposition = short_circuited` → traces where the system
     jumped to answering
   - `re_runnability = not_re_runnable` → traces with hidden steps or
     opaque transformations
   - `tool_grounding = false` → fabricated content
   - `lineage_completeness ≤ 2` → unsourced claims
4. **For each problematic trace**, the expert clicks the rubric buttons
   to add their own scores (often agreeing with or correcting the
   judge), and adds a free-text comment explaining what the system
   should have done instead.
5. **Engineers iterate on prompts and architecture** based on what the
   experts find. The per-user prompt registration (see commit history)
   means each expert can edit their own copy of an agent's prompt and
   see the effect in their next trace's prompt link, so the loop
   between "this trace was bad because X" and "try this prompt change"
   is short.

## What "good" looks like

A target state for this rubric: most traces score `walked_through`, the
conversation reliably produces `versioned_artifact` outputs that live in
a repo, lineage is complete enough that the same workflow could be
re-run without the AI, and the trace itself functions as documentation
another expert could learn from. Each iteration of the agent
architecture is measured against the same rubric, so score distributions
become a stable signal of progress as the system evolves.

When promoting to a production Langfuse instance, recreate these score
configs and judge templates by hand using this doc as the source of
truth. Both resources are UI-only in the current Langfuse public API, so
there's no migration script.
