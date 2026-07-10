# Audit

Self-audit of `campaign-copilot` at commit `a9d2b88` (Phase 6 complete). The method was to
attempt to falsify the project's own claims rather than restate them: read what the README,
`EVAL_REPORT.md` and `docs/threat-model.md` assert, then go and check.

Ten findings. Two are the precise failure mode this project was built to argue against — a
confident, well-formatted claim that nothing supports.

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | **P0** | `EVAL_REPORT.md` claims an LLM judge that does not exist | fixed: clause deleted, replaced with the correction |
| 2 | **P0** | The deployed architecture cannot work: the api never authenticates to the executor | fixed: OIDC token provider + tests |
| 3 | **P0** | `session_id` gives no conversation continuity; `docs/deploy.md` overstates it | fixed: bounded LRU `SessionStore` + tests |
| 4 | **P1** | `python_exec`'s session id is model-controlled: cross-session namespace access | fixed: session is a server-set `ContextVar`, removed from the tool schema |
| 5 | **P1** | `preexec_fn` in a threaded process: documented deadlock hazard | fixed: rlimits applied post-`exec` in `_runner.py`; `start_new_session=True` |
| 6a | **P1** | Rolling summary never runs | fixed: `Agent.run` compresses when the budget demands it |
| 6b | **P1** | Pinned facts are unreachable | **open**: needs a `remember` tool, which changes the action space and needs eval cases first |
| 7 | **P2** | `Settings` reads the environment at import time | fixed: `default_factory` |
| 8 | **P2** | `Metrics.latency_ms` is unbounded and mutated across threads | fixed: `deque(maxlen=1024)` + lock |
| 9 | **P2** | `docs/failure-modes.md` was promised and never written | **open** |
| 10 | **P2** | The multi-turn dataset is loaded and never scored | **open** |
| 11 | **P1** | *Found while fixing P0-3*: a failure in the stream worker's setup hung the SSE response forever | fixed: everything inside the `try`; test added |

---

## P0-1. The eval report claims a judge that does not exist

`EVAL_REPORT.md`, in the section headed *"What this report does not contain"* — the section
whose entire purpose is honesty about gaps — says:

> `cohens_kappa` is implemented and unit-tested against hand-worked examples, **and the judge
> is written**, but the agreement study has not been run.

```console
$ ls src/campaign_copilot/evals/
__init__.py  __main__.py  dataset.py  metrics.py  policies.py  report.py  runner.py
```

There is no `judge.py`. There is no judge. `cohens_kappa` exists and is tested; the claim
attached to it is false.

This is exactly the `wrong_but_grounded` failure the ablation table is about. Every *number*
in the report is reproducible. One *sentence* is not, and it sits in the paragraph asserting
the report's own trustworthiness. A reader who checks the numbers and not the prose comes away
misled, which is the whole mechanism the project claims to defend against.

The fix is one of: write the judge, or delete the clause. Writing a judge in order to make a
sentence true would be the wrong order of operations. **Delete the clause.**

## P0-2. The api cannot call the executor

`deploy/terraform/main.tf` sets the executor to `INGRESS_TRAFFIC_INTERNAL_ONLY` and grants the
api's service account `roles/run.invoker`. Cloud Run enforces that binding by requiring an
OIDC identity token on every request.

```console
$ grep -rn "Authorization\|id_token\|Bearer" src/campaign_copilot/tools/remote_exec.py
(no output)
```

`RemoteSandbox` sends no `Authorization` header. Neither does `/readyz`. On a real deployment
the first `python_exec` call returns **403**, and `/readyz` reports the executor down forever.

`terraform validate` passes. It validates syntax and provider schema. It says nothing about
whether the two services can talk, and I let "validate passes" stand in for "this works" in
`docs/deploy.md`. That table is accurate about what was checked; my *summary* of it was not.

## P0-3. `session_id` is decorative

```python
# service/app.py, inside the per-request worker
memory = ConversationMemory(session_id=body.session_id, budget=...)
```

The memory is constructed **inside the request handler**. Turn two of a conversation gets a
fresh, empty memory. There is no store, no lookup, nothing keyed by `session_id` — the field
is accepted, validated, threaded through, and dropped.

`docs/deploy.md` says:

> **Sessions are in-process.** Two Cloud Run instances do not share `ConversationMemory`, so
> multi-turn breaks the moment it autoscales past one.

That describes a system where sessions work on a single instance. They do not work on any
instance. The known-limitation section understated a total absence as a scaling caveat, which
is worse than not mentioning it: it tells the reader the feature exists.

## P1-4. The model chooses which session's namespace to read

```python
# agent/loop.py
return tool.run(**call.arguments)          # arguments come from the model
```

`python_exec`'s input schema exposes `session_id`. The agent — or anything that has persuaded
the agent — names the session whose Python namespace it reads and writes. In the service, the
*user's* session id goes to `ConversationMemory`; the *tool's* session id is whatever the model
typed.

Two users on one instance, and a model that emits `session_id: "default"` for both, share a
namespace. A prompt-injected model that emits another user's id reads their variables.

The executor is doing exactly what it was asked. The trust boundary is in the wrong place: a
session id is server state, and it should never have been in the model's action space.

## P1-5. `preexec_fn` in a threaded process

`PythonSandbox` passes `preexec_fn=` to `subprocess.run`. The stdlib documents this as unsafe
in the presence of threads — the child runs arbitrary Python between `fork()` and `exec()`,
where only async-signal-safe calls are legal, and a lock held by another thread at fork time is
held forever in the child.

The api service runs the agent under `asyncio.to_thread`. So the local (development)
configuration executes `fork()` with `preexec_fn` from a worker thread, in a process with a
live event loop. It works in testing. It is a documented deadlock, not a theoretical one, and
it will present as a hung request under load.

The rlimits do not need to be set in `preexec_fn`. They can be set by `_runner.py` in the child
*after* `exec()`, where the process is single-threaded and calling `resource.setrlimit` is
ordinary Python. `os.setsid()` has a dedicated, safe flag: `start_new_session=True`.

## P1-6. The rolling summary and pinned facts are dead code

```console
$ grep -rn "\.compress(\|needs_compression\|\.pin(" src/ --include=*.py
(no output)
```

`ConversationMemory.compress()` is written, documented, and tested in isolation. Nothing calls
it. `Agent.run` renders memory and never asks whether it needs compressing, so a long
conversation evicts its oldest turns one at a time until `ContextOverflowError`, and the
"rolling summary" never rolls.

`pin()` is worse. Nothing in the agent or the tools can invoke it. The system prompt does not
mention it. The memory module's headline argument —

> a summary that quietly drops "exclude branded search" produces an answer that is wrong in
> the one way the user specifically asked it not to be

— describes a control that cannot be reached from the running system. The test that proves
pinned facts survive compression is true and irrelevant, because nothing ever pins anything.

I wrote both features, tested them thoroughly, and never connected them. The tests passing is
what hid it: `test_pinned_facts_survive_compression` calls `mem.pin()` directly, so the unit is
green and the wiring is absent. A unit test cannot see a caller that does not exist.

## P2-7. `Settings` freezes the environment at import

```python
@dataclass(frozen=True, slots=True)
class Settings:
    max_steps: int = int(os.getenv("CC_MAX_STEPS", "8"))
```

Dataclass defaults are evaluated once, when the class body runs. Every `Settings()` returns the
environment as it was at import.

```console
env set BEFORE import -> max_steps = 99
env changed AFTER import -> max_steps = 99   # should be 3
```

Harmless in Cloud Run, where the environment is fixed before the process starts. Not harmless
in tests, in a notebook, or under `--reload`, and it is the sort of thing that produces a
forty-minute debugging session exactly once.

## P2-8. `Metrics.latency_ms` grows without bound

`latency_ms: list[float]` accumulates one float per request, forever, and `snapshot()` sorts
the whole list on every `/metrics` scrape. A month of traffic is a slow leak and an
increasingly expensive endpoint. It is also mutated from the worker thread and read from the
event loop with no synchronisation; the GIL makes `+=` on an int *usually* fine and guarantees
nothing.

A bounded `deque(maxlen=1024)` gives the same percentiles and cannot grow.

## P2-9. `docs/failure-modes.md` was promised and never written

The project plan's requirement map cites `docs/failure-modes.md` — twelve documented prompt
failures and their mitigations — against the JD line "understanding of prompt failure modes."
It does not exist. Nothing links to it, so there is no broken reference, but the plan claims
coverage the repo does not provide.

## P2-10. The multi-turn dataset is loaded and never scored

`evals/datasets/multi_turn.jsonl` has six conversations testing pronoun resolution, correction,
and standing instructions. `load_multi_turn()` is exported and imported by exactly one thing:
a test asserting the file parses.

`EVAL_REPORT.md` does say this, to its credit. It is listed here because it is the same root
cause as P1-6: the multi-turn machinery has no scored path, so nobody noticed the multi-turn
features were never wired in. A test that ran those six conversations end to end would have
failed on turn two of every one of them, and would have found P0-3, P1-4 and P1-6 in a single
afternoon.

---

## What the audit says about the tests

221 tests, 88% branch coverage, `mypy --strict`, a regression gate, a mutation-tested
guardrail. All of it green, and all of it blind to three of the four most serious findings.

The reason is uniform: **every one of those tests exercises a unit, and every one of the
misses is a missing edge between units.** `compress()` is tested; nothing calls it.
`RemoteSandbox` is tested against an in-process executor with no auth; production has auth.
`ConversationMemory` is tested; the service builds a new one per request. Coverage measures
lines executed, and every one of these lines executes — just never from the place that matters.

The single highest-value thing missing from this repo is not another unit test. It is one
end-to-end multi-turn conversation against the running service, asserting that turn two knows
what turn one said. It would have caught P0-3, P1-4 and P1-6 at once, and it is roughly thirty
lines.

Those tests now exist (`test_a_session_survives_between_requests`,
`test_two_sessions_do_not_share_memory`,
`test_the_model_cannot_choose_which_session_it_executes_in`).

## A twelfth finding, from the fixes themselves

Repairing P0-3 required rewriting the request worker. The edit was applied with a string
replacement that did not match, because the file had been reformatted since the pattern was
written. `str.replace` returns the original string when it finds nothing. **Nothing failed.**
The tests stayed green, because they were green before the edit and the edit did not happen.

It was caught only by grepping for the symbol afterwards and finding the old code still there.
Then the *actual* fix introduced finding 11: setup code above the `try` in the streaming worker,
which hung the SSE response forever, and which the existing "the stream always terminates" test
could not see because it only ever raised from inside the `try`.

Two edits, two silent failures, in the process of fixing the audit's findings. The lesson is
the same one the ablation table teaches: a green test suite is evidence about the code paths it
executes and about nothing else. Verify the edit landed. Verify the guarantee holds at the
boundary, not in the middle.
