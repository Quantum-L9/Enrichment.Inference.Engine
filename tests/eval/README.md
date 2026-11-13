# tests/eval — quality harness (Campaign B, B4)

Scores field outputs against **labelled synthetic cases** and reports, per
case, which matches are supported by evidence and which are only agreement.

## Claim level: `heuristic_quality`

| The harness reports | The harness does not claim |
|---|---|
| Counts of outcomes per case, on the cases in `cases/` | **Accuracy.** A handful of synthetic cases is not a sample of anything. No ratio is computed or named. |
| Which matches cite evidence that states the value | **Calibration.** A confidence is an uncalibrated score in [0, 1], not a probability. Scores are listed per outcome and never averaged into one. |
| Whether unknown and conflicting labels were kept explicit | **Live outcome.** Nothing here calls a provider or observes a production result. |

## Outcomes

| Outcome | Meaning |
|---|---|
| `evidence_supported_correct` | Equals an evidence-backed label and cites a case evidence item that states that value. |
| `heuristic_agreement` | Equals the label, but the support is not demonstrated: the label is a reviewer expectation (`basis: heuristic`), or the output cites no evidence item stating the value. Agreement is not correctness. |
| `disagreement` / `unit_mismatch` | A different value, or the same value in a different unit. |
| `abstained` | The label has a value (or a conflict) and the output said unknown (or conflicting). |
| `unknown_preserved` / `unknown_coerced` | The label is unknown; the output kept it unknown, or produced a value. |
| `unknown_as_conflict` | The label is unknown and the output reported a conflict: no value was produced, but the output claims disagreeing evidence the case does not contain. |
| `conflict_preserved` / `conflict_coerced` | The label is conflicting; the output kept the conflict, or picked a value. |
| `missing_output` | A labelled field the output omits. Counted, never skipped. |

## Cases

`cases/*.json` are hand-written and synthetic: `evidence` is what each
synthetic source states, `labels` is the expected reading with its `basis`,
`output` is a recorded output under assessment, and `expected` is the outcome
each field must score. The recorded outputs deliberately include failures (a
coerced unknown, a coerced conflict, a wrong unit), so the harness is shown to
separate them. They are **not** EIE outputs: no EIE code path emits per-field
evidence yet. `outputs_from_v1_response` views today's v1 response through the
harness; with no per-field provenance it can reach `heuristic_agreement` at
most.

## Run

    python -m pytest tests/eval -o addopts="" -q
