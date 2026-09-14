# Coverage baseline and ratchet policy

The supported coverage matrix is `just setup` followed by `just coverage`. Setup provisions
the locked Python 3.14 workspace with every supported extra; coverage then runs the
non-integration test suite and measures the shared `common` namespace across both independently
published distributions:

| Distribution | Statements | Covered | Coverage |
| --- | ---: | ---: | ---: |
| `groovemap-runtime` | 2,363 | 2,200 | 93.10% |
| `groovemap-agent-tools` | 118 | 117 | 99.15% |
| **Aggregate** | **2,481** | **2,317** | **93.39%** |

The baseline was measured at `66b65dc` with Python 3.14.7: 638 tests passed and two live
integration tests were deselected. The enforced floor is **93%**, the whole-percent floor of
the measured aggregate. Coverage reports retain two decimal places so drift is visible before
it reaches the gate.

Both `just check` and the reusable CI workflow execute this aggregate coverage command. The
root `pyproject.toml` therefore makes a result below 93% fail locally and in CI, while
`codecov.yml` applies the same fixed project target to the uploaded report. Integration tests
remain a separate capability and do not change this non-integration baseline.

Every workspace package also declares a package-local floor. A repository contract test reads
the workspace member list and rejects any package floor below the aggregate floor, so a local
override cannot quietly weaken the policy. A new workspace package must join the aggregate
`common` measurement and declare a floor at least as strict as the root before the contract can
pass.

The floor is a ratchet: raise it when sustained supported-matrix results justify a new baseline,
and update both package configurations, the fixed Codecov target, the contract constant, and
this evidence table together. Do not lower it to accommodate uncovered changes; add tests or
record an explicit reviewed policy decision with a newly measured baseline.
