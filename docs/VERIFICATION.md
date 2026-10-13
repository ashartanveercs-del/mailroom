# Verification and engineering proof

Verified locally on **2026-10-12**, Windows 11, Python 3.12.10, pytest 9.1.1 and Ruff 0.17.0.
All services were simulated. The results below describe the application under those
controlled inputs, not a live Gmail/Drive deployment or a real model's accuracy.

## Reproduce

From the repository root, after installing `.[dev]` into `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -ra
.\.venv\Scripts\python.exe -m ruff check src tests examples
.\.venv\Scripts\python.exe -m mailroom demo
.\.venv\Scripts\python.exe examples\offline_quickstart.py
.\.venv\Scripts\python.exe -m mailroom eval --json eval-report.json
```

On Linux/macOS, use `.venv/bin/python`. No credentials are needed; `eval-report.json` is
generated and ignored. CI runs the tests, examples and full gate on Python 3.11/3.12/3.13,
and uploads the JSON report. The GitHub matrix was not executed locally.

| Check | Actual result |
|---|---|
| Complete pytest suite | **426 passed**, no skips, exit 0; 53.38 s with the network guard enabled |
| Ruff | **All checks passed**, exit 0 |
| Adapter demo | Exit 0; 9 filed, 1 review, 1 skipped through production adapters and simulated Google APIs |
| Custom-classifier example | Exit 0; one bank statement filed, lunch menu sent to review |
| Full evaluation gate | **27 cells passed, 461 crash points, 0 misfiles, 0 mismatches, 0 violations**, exit 0 |

### Negative controls: verify that the checks can fail

Run this intentionally broken policy against only synthetic data:

```powershell
.\.venv\Scripts\python.exe -m mailroom eval --corpus adversarial --transport memory --mode live --disable cross_check --disable grounding
```

Actual result: **exit 1**, one failing cell, **20 scored misfiles and 10 mismatches**, with no
wire-boundary violations. The misfile score counts wrong document outcomes and stray Drive
paths separately; this does not mean 20 distinct documents were misfiled. This check shows
that a green gate depends on the classification checks rather than a permissive scorer.

The new suite-wide socket/DNS tripwire was also checked with a temporary two-case pytest
probe: a DNS lookup and a socket connection both failed with
`offline test attempted a real network operation` before reaching the network. The probe
was removed after this intentional failure. The complete suite also passed with the guard
enabled; the in-process HTTP simulator requires no loopback socket exception.

## Proof map

| Design property | Implementation | Evidence to inspect |
|---|---|---|
| Mailbox operations are read-only and Drive operations create-only at the request boundary | [`guard.py`](../src/mailroom/guard.py), [`google/policy.py`](../src/mailroom/google/policy.py) | [`test_guard.py`](../tests/test_guard.py), [`test_google_policy.py`](../tests/test_google_policy.py) reject mutation endpoints, method overrides, malformed metadata, hostile pagination and sender-chosen Google upload types |
| Upload identity and intent are durable before bytes are sent | [`filing.py`](../src/mailroom/filing.py), [`ledger.py`](../src/mailroom/ledger.py) | [`test_filing.py`](../tests/test_filing.py) covers pre/post-upload crashes, lost folder-create replies, hash conflicts and late-arriving orphan files |
| A restart does not ask a nondeterministic model to replace an already-committed plan | [`pipeline.py`](../src/mailroom/pipeline.py) | [`test_pipeline.py`](../tests/test_pipeline.py) changes the classifier's answer between crash and restart and checks the original plan is retained |
| Model confidence alone cannot choose a normal filing path | [`routing.py`](../src/mailroom/routing.py), [`classify.py`](../src/mailroom/classify.py) | [`test_routing.py`](../tests/test_routing.py), [`test_classify.py`](../tests/test_classify.py) cover whole-value grounding, role swaps, ambiguous dates and owner-name rejection |
| Hostile containers and ambiguous batch scans remain visible for review | [`containers.py`](../src/mailroom/containers.py), [`documents.py`](../src/mailroom/documents.py), [`split.py`](../src/mailroom/split.py) | [`test_containers.py`](../tests/test_containers.py), [`test_documents.py`](../tests/test_documents.py), [`test_split.py`](../tests/test_split.py) cover size/depth limits, lying extensions and key collisions |
| Recovery, preview and scoring are independently checked | [`evaluation/gate.py`](../src/mailroom/evaluation/gate.py), [`evaluation/corpora.py`](../src/mailroom/evaluation/corpora.py) | [`test_gate.py`](../tests/test_gate.py) checks scorer false negatives, preview writes, every-write crashes and a deliberately broken classification policy |

## Review changes and boundaries

Added a suite-wide socket/DNS guard in [`tests/conftest.py`](../tests/conftest.py), so accidental
real-network tests fail even when retry or fallback code catches ordinary exceptions.
CI now runs the adapter and extension demos explicitly and grants its token read-only
repository permissions. The first-screen README links the gate, proof map and limitations.
No runtime filing defect was found during this review.

- Crash injection throws a `BaseException` before/after simulated writes. It exercises
  application recovery, but does not simulate filesystem corruption, power-loss durability
  or every behavior of Google's live service.
- The model is scripted. The zero-misfile result applies to the 54 synthetic documents and
  configured gate cells; it is not an accuracy rate for arbitrary mail or a hosted LLM.
- The request allowlist governs the provided transport stack. Arbitrary classifier plug-ins
  are Python code, not sandboxed by that allowlist.
- There is no cross-process run lock. Run one instance at a time. Oversized documents,
  image-only scans and ambiguous splits have the documented review/skip limitations.
- The ledger stores plaintext names, hashes and folder identifiers. Sender filters are not
  email authentication. See [security notes](../README.md#security-notes) and [limits](../README.md#limits).

## Public-release audit

Reviewed the 69 tracked files and 105 unique historical text blobs reachable from the two
existing commits. Enumeration used `git ls-files`, `git rev-list --objects --all` and
`git cat-file --batch`; attribution used
`git log --all --format="%h %an <%ae> | %s"`.

Pattern scans and source/example review found no credential markers, private workspace or
infrastructure identifiers, or non-example email addresses in file contents. Fake credential
and authorization fixtures are intentionally synthetic. OAuth tokens, local configuration,
databases, `.env` and gate reports are ignored. Maintainer name and email remain visible in
Git authorship metadata. No release blocker was identified and no history was rewritten;
the scan is heuristic rather than a certification that no sensitive content can exist.
