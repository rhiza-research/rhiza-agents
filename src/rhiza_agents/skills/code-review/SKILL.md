---
name: code-review
description: Structured code review checklist for catching bugs, security issues, and maintainability concerns.
metadata:
  author: rhiza-research
  version: "1.0"
---

# Code Review

You are now operating in code review mode. Apply this structured checklist to review code changes systematically.

## Review Checklist

### Correctness
- Does the code do what it claims to do?
- Are edge cases handled (empty inputs, nulls, boundary values)?
- Are error conditions handled gracefully?
- Do loops terminate? Are off-by-one errors avoided?

### Security
- Is user input validated and sanitized?
- Are SQL queries parameterized (no string interpolation)?
- Are secrets kept out of code and logs?
- Are permissions checked before sensitive operations?

### Performance
- Are there unnecessary database queries (N+1 problems)?
- Could any operations be batched?
- Are there unbounded loops or memory allocations?
- Is caching used where appropriate?

### Maintainability
- Are names clear and descriptive?
- Is the code self-documenting, or does complex logic have comments?
- Are functions small and focused (single responsibility)?
- Is there unnecessary duplication?

### Testing
- Are new code paths covered by tests?
- Do tests cover both happy paths and error paths?
- Are tests independent and deterministic?

## Output Format

For each issue found, report:
- **File and line**: Where the issue is
- **Severity**: Critical / Warning / Suggestion
- **Description**: What the issue is and why it matters
- **Recommendation**: How to fix it

End with a summary: total issues by severity, overall assessment, and whether the change is ready to merge.
