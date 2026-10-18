# Throwaway ablation evidence for issue #5210

Question: which error-handling branches or abstractions can be removed without breaking safe HTTP responses, underlying diagnostic logs, or existing business-error responses?

Baseline: `10f321b87f53ad750a4d5e9525047a017d7f8abd`. Full auth and handlers tests passed before the experiment. Each variant changes one implementation dimension and restores production files afterward. Tests use HTTP handlers with temporary real SQLite databases (closed handles, missing tables, injected write failures), including the compatibility-test patch. Tests are not weakened between variants; helper signature adaptation is the only test edit.

| Variant | Result | Production line delta | Failed leaf cases |
| --- | --- | ---: | ---: |
| login-generic | FAIL | -7 | 3 |
| login-text-classification | FAIL | +0 | 2 |
| invitation-no-validation | FAIL | -9 | 11 |
| last-manager-generic | FAIL | -8 | 3 |
| invitation-collapse-lookup | FAIL | -7 | 4 |
| no-diagnostic-logs | FAIL | -5 | 51 |
| reuse-user-lookup | PASS | -6 | 0 |
| unify-invitation-handler | PASS | -14 | 0 |
| shared-internal-error | PASS | -7 | 0 |
| combined | PASS | -20 | 0 |

Decision: adopt `combined`: reuse `writeUserLookupError` for password resets and merge invitation creation into `writeInvitationError`, passing a fixed operation message. This removes 20 production lines and one helper while retaining all 96 passing test/subtest results. Keep sentinel classification, explicit invitation lookup error propagation, and diagnostic logs. Text classification confuses SQLite failures whose text matches business rejection messages. Dropping classification breaks 400/401/409 compatibility; collapsing invitation lookup hides store failures as 410. Removing logging fails diagnostic assertions.

A generic internal-error helper passes but only saves 7 lines while adding an abstraction and splitting direct log/response handling across functions; do not adopt it.

The production PR keeps only the validated changes and compatibility regression assertions. Experiment code, failed variants, and evidence stay on this throwaway branch.

## Reproduce

Check out this throwaway branch in an isolated worktree. The branch contains the baseline implementation, the added compatibility tests, and the runner. From the repository root:

```sh
python3 dashboard/backend/auth/ablation_prototype/run.py
```

The runner starts every variant from the baseline production sources, keeps the regression assertions constant, and restores the checkout's original files after each run.

See each `*.result.json` for failing cases and each `*.patch` for the exact mutation. Diagnostic logs stay local because they include temporary filesystem paths. The no-log variant's raw line delta is affected by formatting that retains blank lines; the test verdict is the useful metric.

Final validation of the selected implementation: complete auth and handlers race tests passed, as did the changed-file `make check` (including 1,141 frontend tests and all 18 backend packages). The final fixed-SQL collision fixtures were rechecked under race after lint cleanup.
