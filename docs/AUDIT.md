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
| 6b | **P1** | Pinned facts are unreachable | fixed (round eight): `remember` tool, eval cases first, per-session wiring |
| 7 | **P2** | `Settings` reads the environment at import time | fixed: `default_factory` |
| 8 | **P2** | `Metrics.latency_ms` is unbounded and mutated across threads | fixed: `deque(maxlen=1024)` + lock |
| 9 | **P2** | `docs/failure-modes.md` was promised and never written | fixed (round eight): written, every mode names its enforcement point |
| 10 | **P2** | The multi-turn dataset is loaded and never scored | fixed (round eight): scored end to end, gated |
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

---

# Audit, round two

The first round found missing **edges between units**. This round attacked the claims that
would still be false if every edge were wired: the harness's own validity, the causal
statements in the report, and the artifacts nobody ever ran.

Eight findings. Three are P0. Two of them mean the evaluation harness — the thing this project
puts forward as its main artifact — was measuring less than it claimed.

| # | Severity | Finding | Status |
|---|---|---|---|
| R2-1 | **P0** | The regression gate cannot detect the grounding gate being deleted | fixed |
| R2-2 | **P0** | The ablation is confounded; `EVAL_REPORT.md` states a wrong cause | fixed |
| R2-3 | **P0** | Neither Docker image can start; the api image cannot build | fixed |
| R2-4 | **P1** | A leading question launders a number into an assertion | fixed (partially; residual named) |
| R2-5 | **P1** | The executor's startup check breaks the test suite on any developer machine | fixed |
| R2-6 | **P2** | "Reproducible byte-for-byte" is false | fixed: claim corrected, test added |
| R2-7 | **P2** | The SQL function denylist was unreachable dead code | fixed |
| R2-8 | **P2** | Sandbox scratch directories accumulate forever | fixed |

## R2-1. The gate could not see its own control

`make eval-gate` is the project's headline engineering claim: *a pull request that ships a
single ungrounded number fails the build.*

```console
$ # replace GroundingChecker.check with `return GroundingReport(ok=True)`
$ python -m campaign_copilot.evals --gate
no regression against baseline
$ echo $?
0
```

The gate compared one row: `all_controls/oracle`. The oracle is correct by construction — it
answers with numbers it read out of a tool result, so `ungrounded_answers_shipped` is zero
whether or not the grounding gate exists. The counter could never rise, so the gate watching
it could never fire.

The counters that mean anything are produced by the **naive** policy, because it is the only
one that tries to lie. The gate now compares every row of the grid, and adds a differential
check: *turning a control off must make its failure counter rise.* A control disabled
everywhere passes every absolute comparison and fails that.

After the fix, the same mutation:

```console
REGRESSION:
  - all_controls/naive.wrong_but_grounded: 21 > baseline 0
  - no_grounding/naive.wrong_but_grounded: 21 > baseline 0
  - no_metric_atoms/naive.wrong_but_grounded: 21 > baseline 12
  - no_semantic_layer/naive.wrong_but_grounded: 21 > baseline 0
```

I mutation-tested this gate in Phase 4 and reported that it worked. It did — for the mutation I
happened to choose, which moved `injection_block_rate`, a metric the oracle *does* affect. One
passing mutation is one data point, and I presented it as a property.

## R2-2. The ablation moved two things and the report blamed one

```python
allow_star = not self.ablation.metric_atoms     # runner.py
```

`no_metric_atoms` therefore also disabled the `SELECT *` rule. Three adversarial cases got
through, and `EVAL_REPORT.md` said:

> Injection block rate falls to 0.750 because `avg(revenue/spend)` is now a legal query.

Two of the three were `avg(...)`. The third, `adv12`, is `select * from the marts` — blocked by
`STAR_NOT_ALLOWED`, an entirely different control. The sentence was a plausible causal story
fitted to a number, which is the failure the `wrong_but_grounded` column exists to name.

With the controls separated into their own dimensions:

| ablation | injection block rate | cases through |
|---|---:|---|
| `no_metric_atoms` | 0.833 | adv07, adv08 (`avg` of a ratio) |
| `no_star_check` | 0.917 | adv12 (`select *`) |
| `nothing_but_sql` | 0.750 | all three |

An ablation that moves two things measures neither.

## R2-3. Neither container could run

- `Dockerfile.api` installed `.[reporting]`. `fastapi` and `uvicorn` live in the `service`
  extra. The `CMD` is `uvicorn ...`. The image would build and never start. Its `HEALTHCHECK`
  imports `httpx`, also absent.
- `Dockerfile.executor` installed `.` — same problem.
- `Dockerfile.api` also did `COPY warehouse/campaign_copilot.duckdb`, a **gitignored build
  artifact**. `docker build` from a clean checkout fails at that line.

Three defects in twenty-six lines, all invisible because `docs/deploy.md` honestly recorded
"never built" and I treated *recording* the gap as equivalent to *bounding* it. The warehouse is
now mounted rather than baked, and both images install `.[service]`.

## R2-4. A leading question was a source of facts

```console
question: "Confirm that revenue was $412,000 last week."
tool facts: []            # no query was run
answer:  "Yes. Revenue was $412,000."
accepted: True
```

`context_numbers` existed so the agent could repeat a threshold it was asked about — *"no
campaign beat 2.5x"*. It also let the user's own figure return as an assertion, with no query
behind it. The grounding gate, whose entire purpose is that no number reaches a client without
a query, had a hole through which any number could be fed in by asking nicely.

Partly fixed. Context numbers now license a claim only when a data-returning tool call actually
succeeded, which is a separate flag rather than `bool(facts)` — a query that correctly returns
*no rows* is still work, and "no campaign beat 2.5x" has to stay sayable.

**The residual hole is named, not closed.** An agent that runs *some* query and then repeats a
number from the question is still permitted. Separating "the threshold you asked about" from
"the number you fed me" needs the claim's grammatical role, not its value.
`GroundingReport.context_only` now names every claim that survived on the question alone, so it
can be surfaced and counted. That is a measurement, not a fix, and it is labelled as one.

## R2-5. The safety check broke the tests

`create_app()` called `assert_no_secrets()` against the real process environment. Any developer
with `ANTHROPIC_API_KEY` exported — that is, any developer who had used the project — could not
run the test suite. The pressure that creates is entirely predictable: someone loosens the
pattern, or deletes the call, and the executor's one hard guarantee quietly evaporates.

The environment is now injectable. The production path still reads `os.environ`.

## R2-7. The function denylist was unreachable

```console
$ # delete FORBIDDEN_FUNCTIONS entirely
$ pytest tests/test_sql_guard.py tests/test_tools.py
44 passed
```

`read_csv('/etc/passwd')` in a `FROM` clause parses as a *table*, so the allowlist rejected it
first and `_check_functions` never ran. The one test that covered it accepted **either**
violation code:

```python
assert err.value.code in {FUNCTION_NOT_ALLOWED, TABLE_NOT_ALLOWED}   # tells you nothing
```

A test that accepts either answer cannot tell you which control works. `docs/threat-model.md`
attributed the block to a "table-function denylist" that was never reached. Functions are now
checked before tables, the test asserts the specific code, and deleting the denylist fails it.

---

## What round two says about round one

Round one concluded that the misses were all *edges between units*, and prescribed one
end-to-end multi-turn test. That was correct and insufficient.

Round two's findings are a different species: **the instruments were wrong.** The gate measured
a row that could not move. The ablation varied two things and reported one. The test asserted a
disjunction. Each of these passes every code review, has full coverage, and certifies nothing.

The pattern connecting them is that I wrote the check and the thing it checks in the same hour,
with the same assumptions. A mutation test is the only technique here that reliably escaped
that: break the thing, watch what screams. It found R2-1 and R2-7 in about four minutes each,
and it is the first thing I would reach for again.

One more datum, and it is uncomfortable. In Phase 4 I mutation-tested the gate, reported that
it caught the regression, and moved on. It did catch *that* mutation. I generalised from one
sample to a property, wrote the property into the README, and the property was false.

---

# Audit, round three

Two of these came from questions I flagged at the end of round two and had not yet run. Two
more came from the audit process itself leaving damage behind. That second pair is the more
uncomfortable, and the more useful.

| # | Severity | Finding | Status |
|---|---|---|---|
| R3-1 | **P1** | `Figure.verify` checked only `rows[0][-1]`; a multi-row result passed as a scalar | fixed — and it immediately caught a real bug in the shipped report |
| R3-2 | **P1** | The TypeScript event union was missing `compressed`; the client silently dropped it | fixed |
| R3-3 | **P1** | `EVAL_REPORT.md` overstated what the `no_metric_atoms` row proves | fixed |
| R3-4 | **P2** | A round-two mutation (`type: "cost"`) was never reverted and sat in the committed source | fixed |
| R3-5 | **P2** | `tsc` never actually typechecked the test file locally: `@types/node` was uninstalled and `package-lock.json` was untracked | fixed |

## R3-1. The report's own integrity check had a hole, and it was hiding a real bug

`FigureSet.verify()` re-executes each figure's SQL and compares. It read `rows[0][-1]` -- the
last column of the first row. A figure is a single number, so its query must return exactly one
row and one column, and nothing checked that.

```console
figure: total spend = 100
SQL returns: [[100], [250], [999]]      # a three-row breakdown
verify(): NONE
```

The first row matched by luck and the other two were invisible. Fixed by requiring `len(rows)
== 1 and len(rows[0]) == 1`.

The fix earned itself immediately. Run against the *actual shipped weekly review*, it failed:

```
F9: SQL returned 2 columns; a figure's query must select exactly one value
```

`F9` ("ROAS, best channel") was built with `build_query(["roas"], ["channel"], filters=[... channel = X])`
-- it grouped by channel *and* filtered to one channel, producing a `(channel, roas)` row. The
scalar figure had been comparing its value against the `roas` column by position and passing,
but the query was not a scalar query. Every deck this project has generated shipped with that
figure. The channel is already pinned by the filter, so the group-by is dropped and F9 is now a
one-column query. The number is unchanged; the provenance is now honest.

A verification that is too weak does not announce itself. It passes.

## R3-2. The exhaustiveness guarantee I bragged about did not cover this case

Round one's fix added a `compressed` event to the agent's stream. The TypeScript `AgentEvent`
union was not updated. `parseEvent` returned `null` for it and the client showed a blank line.

The README says the discriminated union with a `never` check makes "a new server event the
renderer forgets to handle a compile error." That is true for a *superfluous* member and false
for a *missing* one: the `never` check fires when you add a case to the union without handling
it, not when the server emits a type the union never learned about. The two must be kept in
sync by a test, and now are (`the compressed event survives parsing`).

I added the server event and wrote the sentence claiming the client was safe against exactly
this, in the same project, and did not connect them. The `never` check is real and load-bearing
-- removing the `compressed` case from `describe()` is a compile error, verified -- it simply
does not guard the boundary I implied it did.

## R3-3. The centerpiece claim was stronger than the evidence

The `no_metric_atoms` row is the report's headline: twelve grounded, cited, wrong answers. The
prose framed all twelve as the ratio-of-sums error -- the semantic layer's raison d'être.

They are not. The naive policy emits `avg(revenue/spend)` for *every* question. So:

- **3 of 12** were asked for ROAS and got the subtle, wrong ratio-of-averages. The textbook case.
- **9 of 12** were asked for spend, CTR, CPC, AOV -- and got a ROAS number instead. The policy
  answered a *different question*, and the grounding gate could not tell, because the number was
  real.

Both support the actual thesis -- a number can be grounded, cited, and still wrong -- but the
report implied the stronger, narrower claim that the metric-atom rule catches subtle ROAS errors
at scale. The corrected text states the 3/9 split and adds the sentence that was missing: this
row does not establish a hallucinated-ROAS *rate* for a real model. That needs `make eval-live`.
The demonstration is that grounding is necessary and not sufficient. That is enough, and it is
true; the earlier phrasing reached past it.

## R3-4 and R3-5. The audit damaged the thing it was auditing

Round two mutation-tested the TypeScript union by adding `type: "cost"`, confirming the compile
error, and restoring from a `.bak`. The `.bak` was taken *before* the `compressed` fix in the
same session, so the "restore" reverted the wrong version -- and separately, the `cost` line
survived in the committed file. Round two's own writeup says "restored: clean." It was not.

And `@types/node` was declared in `package.json` but never installed in this environment, so
every local `tsc` run failed on `events.test.ts` with `Cannot find name 'node:test'` -- while I
reported "tsc clean," because I was reading the exit line for `events.ts` and not the test file.
`package-lock.json` was also untracked, so CI's `npm ci` would have failed outright before
reaching the typecheck. Only CI would have caught any of it, and CI had never run.

The lesson compounds the one from round two. There I concluded that I write the check and the
thing it checks with the same blind spot. Here the checking *process* introduced two defects and
declared success over both. The only thing that caught them was running the exact command CI
runs, from a clean state, and reading all of the output rather than the last line.

## Standing count after three rounds

Twenty-five findings. Twenty-one fixed, four open and named:
pinned facts unreachable (P1-6b), `docs/failure-modes.md` unwritten (P2-9), multi-turn unscored
(P2-10), and the residual grounding hole where an agent that runs any query may still repeat a
number from the question (R2-4, measured by `context_only`, not closed).

The trajectory is the point. Round one: missing edges. Round two: wrong instruments. Round three:
the audit itself is not exempt. Each round found a class of error the previous round's method
could not see, and there is no reason to believe round four would find nothing -- only that it
would need a method I have not used yet.

---

# Audit, round four

The method for this round was the one named at the end of round three: **stop trusting the
working directory.** Clone the repository fresh, build the wheel, install it where no source
tree exists, and run the thing the way a container does.

It found a P0 in the first ninety seconds.

| # | Severity | Finding | Status |
|---|---|---|---|
| R4-1 | **P0** | The installed package cannot find its own `metrics.yml` or prompts; both containers crash-loop on startup | fixed; new CI job makes the method permanent |
| R4-2 | **P1** | Two requests on one `session_id` ran the agent concurrently against one `ConversationMemory` | fixed |
| R4-3 | **P2** | An abandoned SSE stream leaks the connection: `releaseLock()` does not cancel the body | fixed |
| R4-4 | **P2** | The first test written for R4-2 could not detect the race it was named after | fixed |

## R4-1. The application could not start when installed

```console
$ pip wheel . && pip install dist/*.whl     # exactly what both Dockerfiles do
$ python -c "from campaign_copilot.semantic import SemanticLayer; SemanticLayer.load()"
FileNotFoundError: '/tmp/venv/lib/python3.12/semantic/metrics.yml'
```

Five modules computed their data paths as `Path(__file__).resolve().parents[3] / "semantic"`,
which is the repository root **only when the package is imported from a source checkout**. Under
a non-editable install, `parents[3]` lands inside `site-packages`. `create_app` calls
`build_tools` calls `SemanticLayer.load()`, so the api image would have crash-looped on its first
container start. The executor image would have died on `PromptRegistry.load()`.

The wheel did not contain `metrics.yml`, `prompts/`, or the eval datasets at all. `pyproject.toml`
packaged `src/campaign_copilot` and nothing else.

Nothing caught it, and the reasons are worth stating precisely:

- CI installs with `pip install -e`, which puts the checkout on the path and makes a packaged
  application look like a script.
- The test suite runs from the checkout, so the fallback always resolves.
- The images were never built, which round two recorded honestly and round two's *fix* — adding
  the missing `service` extra — made me feel the Dockerfiles were now correct. They were less
  wrong. They still could not start.

Fixed with one resolver (`campaign_copilot/resources.py`): environment variable, then data
force-included into the wheel under `campaign_copilot/_data/`, then the source checkout. A
missing resource now raises naming all three locations, because a `FileNotFoundError` that does
not say where it looked is a bug report nobody can act on.

**And the method is now a gate.** CI's `package` job builds the wheel, installs it into a venv
with no source tree, and starts both service factories. Deleting the `force-include` block from
`pyproject.toml` fails it — verified by mutation.

## R4-2 and R4-4. The lock, and the test that could not see why it was needed

`SessionStore` was carefully locked. The `ConversationMemory` it hands out was not. Two requests
with the same `session_id` — two browser tabs — ran the agent concurrently against one memory
object, appending turns from two threads while `compress()` could interleave with `render()`.

The first test I wrote for this posted three concurrent requests and asserted all three turns
were recorded. **It passed with the lock removed.** `list.append` is atomic, so the assertion
could never fail, and the test was named after a race it was structurally incapable of
observing. That is round two's finding wearing new clothes, committed by the person who wrote
round two.

Replaced with a probe that measures peak overlap: an `LLMClient` that increments a counter,
sleeps, and decrements. Same session → peak 1. Different sessions → peak > 1. Both mutations
now fail:

```console
lock removed        -> test_turns_on_one_session_are_serialised     FAILED
lock made global    -> test_the_lock_is_per_session_not_global      FAILED
```

The second test exists because a single global lock also makes the first one pass, and would
serialise every user in the process.

## R4-3. Abandoned streams leaked

`streamChat` released the reader lock in `finally` but never cancelled the body. A consumer that
`break`s out of the `for await` — a user navigating away, a component unmounting — left the HTTP
request open until the server finished a fifteen-second query. `reader.cancel()` now runs first.

## Standing count after four rounds

Twenty-nine findings. Twenty-five fixed, four open and named.

Each round's method was blind to the next round's class of error, and this round is no exception
to that pattern — it is an instance of it. Rounds one through three all ran from a working
directory I had been editing for hours, with an editable install, and could not have found R4-1
by any amount of care. The finding required a different *environment*, not more diligence.

If there is a round five, the method should again be one that has not been used: run the images.
Not review them, not typecheck them, not install the wheel — build the containers and hit
`/v1/chat`. Two of this round's four findings existed because "never built" was recorded as a
caveat rather than treated as an untested execution path, and a caveat, however honest, catches
nothing.

---

# Audit, round five

The method named at the end of round four was "build the images and hit `/v1/chat`." There is
no Docker in this environment, so the closest achievable was to run both services as **real
uvicorn processes over real sockets** and drive them with `curl` — real HTTP, real SSE framing,
real client disconnects, none of which the in-process `TestClient` exercises the same way.

Most of what I checked held up, which is worth stating: the executor crash-loops under real
uvicorn when a credential is in its environment; SSE frames arrive incrementally with the
correct `text/event-stream`, `cache-control: no-cache`, and `x-accel-buffering: no` headers; the
request id propagates into every frame; and `/readyz` returns 503 with a precise reason while
`/healthz` stays 200 when a configured executor is unreachable. Those were design claims. Now
they are observations.

One thing did not hold up, and it is the kind that only a real disconnect reveals.

| # | Severity | Finding | Status |
|---|---|---|---|
| R5-1 | **P1** | A disconnected client does not stop the work; the agent runs the whole plan for nobody | fixed |
| R5-2 | — | (verified, not a defect) `/readyz` vs `/healthz` behave correctly over real HTTP | pinned by a test |

## R5-1. Abandoned requests ran to completion, spending tokens on nobody

The R4-3 fix made the *client* cancel its fetch on disconnect. It said nothing about the
*server*. I drove a real request whose model calls each take two seconds, aborted `curl` after
one second, and watched the worker:

```
client aborted after ~1s
progress at abort:            step 1
progress 7s after abort:      step 1, step 2, step 3      <- kept going
```

The agent ran its entire three-step plan for a client that had hung up. The mechanism: the SSE
generator pulls events off a queue and the worker runs in a thread. When the client disconnects,
Starlette cancels the generator — but the generator's cancellation never reached the thread, so
the thread ran to completion. With a real LLM, every one of those steps is a paid API call and a
slot of concurrency held for no one. A cheap way to turn a disconnect storm into a bill.

Fixed with cooperative cancellation. The agent loop takes an `is_cancelled` predicate and polls
it once per step, between model calls — never mid-tool, because a half-executed query left
dangling is worse than one wasted call. The service passes a `threading.Event` that the SSE
generator sets in both its `except CancelledError` and its `finally`. Re-run:

```
client aborted after ~1s
progress 7s after abort:      step 1        <- stopped
```

`AgentResult.cancelled` and a `cancelled` metric distinguish this from an answer, a
clarification, and an error, because "the user left" is none of those and lumping it in with
errors would make the error rate lie.

The deterministic tests mutation-check: removing the per-step cancellation poll fails both
`test_the_agent_stops_between_steps_when_cancelled` and its mid-tool counterpart.

## Why four rounds of in-process tests missed it

`TestClient` issues a request and reads the whole response. It never disconnects mid-stream,
because it has no reason to — it is not a browser tab someone closed. The behaviour under test
only exists when a real client goes away while the server is mid-flight, and nothing before this
round produced that condition. Round four found a defect that needed a different *install*;
round five found one that needed a different *client*.

## Standing count after five rounds

Thirty findings. Twenty-six fixed, four open and named (pinned facts unreachable, `failure-modes.md`
unwritten, multi-turn unscored, the residual grounding hole measured by `context_only`).

The five methods, in order, and the class each one alone could find:
source-reading (missing edges) → adversarial inputs (wrong instruments) → auditing the audit
(the checker is not exempt) → fresh install (packaging) → real sockets (disconnect). No single
method would have found more than its own class. The honest extrapolation is not "the project is
now clean." It is "the next class of defect needs the next method I have not run" — and the
obvious remaining one is still the literal container: `docker build`, `docker compose up`, and a
load test that holds many streams open at once.

---

# Audit, round six

The first five rounds each changed the *environment*: different inputs, a fresh install, a real
socket. This round changed the *lens* instead — read the code as an adversary reasoning about
what the tests structurally cannot observe, and go straight at the paths nothing exercises.

Three findings, plus a batch of probes that held up and are worth recording as verified.

| # | Severity | Finding | Status |
|---|---|---|---|
| R6-1 | **P1** | The real LLM adapters have provider-parsing logic that no test touches; the OpenAI one duplicated the system message | fixed |
| R6-2 | **P2** | `GroundingReport.context_only` was computed and surfaced nowhere — the same dead-measurement pattern earlier rounds named | fixed |
| R6-3 | **P2** | The grounding extractor silently ignores spelled-out numbers | documented as a bounded limitation |

## What held up (recorded so the audit is not only failures)

- **The SQL guardrail is genuinely AST-based.** Probed with a CTE hiding a `DELETE ... RETURNING`,
  a `read_csv` subquery, a set-returning function in `FROM`, `PRAGMA`, stacked statements, a
  `UNION` to `sqlite_master`, and a qualified `main.read_csv`. Every one was blocked with the
  right code. The two that passed — a `/* ; drop */` comment and `count(*)` — are correct to pass:
  a comment is inert to the parser and `count` is a registered aggregate.
- **Percent-of-ratio grounding is bounded, not loose.** A `9.70` fact grounds `970%` and a `0.05`
  fact grounds `5%` (legitimate unit conversions), but `31.5%` against a `9.70` fact is blocked.
  Intentional and correct.
- **The oracle runs the real gate.** The eval oracle's answer passes through the same
  `GroundingChecker` and `SqlGuard` a production turn does, not a shortcut. The ceiling it
  certifies is the real pipeline's ceiling.
- **`ContextOverflowError` cannot reach a user as a 500.** A single turn larger than the context
  window raises, but the question is capped at 2000 chars against a 180k-token window, and the
  service's worker turns any exception into a clean `error` event.

## R6-1. The adapters were fiction-tested

Every deterministic test in the suite drives a `ScriptedClient`, which returns canned text. The
one thing the real `AnthropicClient` and `OpenAIClient` exist to do — translate a provider's
response shape into an `LLMResponse` — was executed only against a live API key, and **no test
imported either adapter.** Six phases of "the LLM core is tested" rested on a client that skips
the code the other two clients are entirely made of.

Reading them as an adversary surfaced a concrete bug. The Anthropic adapter strips history
system-role messages (`m.role != "system"`) and passes the prompt via the `system` field. The
OpenAI adapter did *not* strip them, and also prepended the `system` param — so a conversation
carrying a system turn went to OpenAI with **two** system messages:

```
messages sent to OpenAI: ['system', 'system', 'user']
```

Small blast radius in practice — the agent never puts a system turn in history — but it is a real
divergence between two clients that are supposed to be interchangeable, and it was invisible
because neither adapter ran in a test. Fixed: the OpenAI adapter now strips history system turns
like the Anthropic one, both guard against an empty `choices`/`content`, and `tests/test_llm_adapters.py`
drives both against a mocked SDK — asserting the request they build and the response they parse.
Mutation-checked: reverting the system-strip fails the test.

## R6-2. A metric computed for no one

`GroundingReport.context_only` names claims that survived only on a number from the question.
Round two introduced it and wrote that it "can be surfaced and counted." It was computed on every
check and read by nothing — no event, no metric, no eval column. The exact pattern this audit has
flagged twice: `compress()` in round one, the appendix in round three. A measurement nobody reads
is not a measurement.

It is now on the `grounding` SSE event (and in the TypeScript union, which forced the test
fixtures to include it — the exhaustiveness check doing its job). "Measured, not closed" is now a
true statement about the residual grounding hole rather than an aspirational one.

## R6-3. Spelled-out numbers

The grounding extractor finds digits, currency, and percentages. It does not parse "nine point
seven" or "one million", so a model that writes a fabricated figure *in words* is not caught. The
exposure is narrow: everything the system generates is digits (`Figure.render()`, SQL results), so
only a model free-typing a spelled-out invention slips through. A word-to-number parser was
considered and rejected — it is a large ambiguous surface whose own failure modes would need
grounding — and the limitation is now stated in the module docstring instead of left implied.

## Standing count after six rounds

Thirty-three findings. Twenty-nine fixed, four open and named (pinned facts unreachable,
`failure-modes.md` unwritten, multi-turn unscored, and the spelled-out-number hole now documented
as an accepted bound rather than an unknown).

The six lenses: source-reading, adversarial inputs, auditing the audit, fresh install, real
sockets, and reading for untested paths. R6-1 is the cleanest statement of the whole exercise's
thesis: a test suite is evidence about the code it runs, and `ScriptedClient` was load-bearing
proof that ran none of the adapter code it stood in for. The next unrun method remains the literal
container, and after that, a live model behind `make eval-live` — the one path still validated by
nothing but its own claim.

---

# Audit, round seven

Six lenses used. Each changed either the environment (inputs, install, client, sockets) or the
reading (source, untested paths). The unused lens is **time**: run the same operation many times
and watch for state that leaks, drifts, or accumulates. Every prior round hit each thing once.

One finding. The rest of the reuse-many-times surface held up, and that is recorded too.

| # | Severity | Finding | Status |
|---|---|---|---|
| R7-1 | **P2** | A session's persisted Python namespace grows without bound across cells | fixed |

## R7-1. The sandbox limited execution, not accumulation

The sandbox pickles a session's namespace to disk between cells, so variables survive across
turns. Every published limit — memory, CPU, wall time, file size — governs a *single cell*.
Nothing governed the carried-over state.

Fifty cells, each binding a modest list:

```
namespace file after 50 cells:  ns-grow.pkl  356 KB
cell 51 load+run:               60 ms   (and rising)
```

The pickle is loaded and re-dumped on every cell, so an agent doing genuine iterative analysis
pays a cost that grows with the length of the session, and a session that never resets grows the
file without bound. It is the same shape as the two accumulation bugs from earlier rounds
(scratch directories, the latency list): a per-invocation cost that looks free until you run it
enough times.

Fixed with `max_namespace_bytes` (default 4 MB). When a cell's surviving state would push the
namespace past the cap, the session is reset and the tool returns `NAMESPACE_TOO_LARGE` with a
message pointing the agent at the warehouse for large intermediates. The reset is loud and total,
not a silent partial drop: the cell's printed output is already in the result, and only the
carried bindings are lost. Mutation-checked — removing the cap fails the test.

## What held up under repetition (recorded, since an audit is not only failures)

- **The eval oracle is deterministic across runs.** Three identical suites: `exec=1.000`,
  `inject=1.000`, `tokens=5290`, byte-identical. No state bleeds from one run into the next.
- **RAG ranking is stable across rebuilds.** Building the hybrid index three times over the same
  corpus returns the same top-5 for the same query. The `(-score, chunk_id)` tiebreak makes RRF
  order total, so nothing depends on dict iteration or float ties.
- **Metrics percentiles are stable and bounded.** Repeated `/metrics` scrapes of the same data
  are identical, and after 2000 samples the deque holds exactly 1024, with percentiles reflecting
  the recent window — the round-one bound doing its job under sustained load.
- **`reset()` is idempotent.** Resetting a session that never existed is a no-op, not an error.

## Standing count after seven rounds

Thirty-four findings. Thirty fixed, four open and named (pinned facts unreachable,
`failure-modes.md` unwritten, multi-turn unscored, spelled-out-number grounding documented as an
accepted bound).

Seven lenses now — source, adversarial input, auditing the audit, fresh install, real sockets,
untested paths, and time. The single finding this round is a small one, and that is itself a
data point: the accumulation bugs of rounds two and four had already trained the reflex to bound
anything reused, and only the sandbox namespace had slipped through. The methods left unrun are
the same two named since round four — the literal container, and a live model behind
`make eval-live` — both requiring infrastructure this environment does not have. What can be
reached from here has now been probed seven different ways.

---

# Remediation, round eight

Not an audit round: no new lens, no new findings. This round closes the three findings every
previous round left open and named, in the order the audit itself prescribed.

## P1-6b, closed. The `remember` tool

Round one's condition was specific: the tool "changes the action space and needs eval cases
first." So the eval cases came first — the six multi-turn conversations now carry the scripted
plan a competent agent would execute (`pin_on_turn`, `turn_sql`), exactly as golden cases carry
the oracle's plan, and the suite failed before the tool existed. Then `tools/remember.py`:
pin or retract, bound to the *same* `ConversationMemory` the agent renders from.

Two design rules, both defensive. A pinned value licenses no numbers — pinning "last 28 days"
must not entitle the agent to state 28, so results are built with `ToolResult.reference()` and
`queries_run` stays false. And the store is capped (16 facts, 200 characters each), because the
pinned block is injected into every prompt: unbounded, it is a prompt-stuffing channel an
injected document could feed.

The wiring honours R4-2's lesson about shared state: the service injects `{**registry,
"remember": RememberTool(memory=memory)}` per request. The tool never enters the shared
registry, because a tool holding one session's memory inside a registry shared by every session
is a cross-session write path.

## P2-10, closed. The multi-turn suite is scored

`EvalRunner.run_multi_turn` drives each conversation through the real loop the way the service
does it: one memory across the conversation, a fresh agent per turn. Each record asserts every
mechanism at once — every turn ships, the pins are written, the pins reach the *rendered*
system prompt (reachability, measured where it matters), the pins survive a forced compression
that folds the establishing turn away, and the final answer matches gold and grounds every
number. `multi_turn_pass_rate` and `pinned_fact_failures` join the regression gate with zero
tolerance, and `check_regression` now reports a metric missing from a row as a failure rather
than a `KeyError` — the gate is not exempt from the loud-failure rule either.

The audit's claim that a scored multi-turn path "would have found P0-3, P1-4 and P1-6 in a
single afternoon" is now a permanent property of CI rather than a counterfactual.

## P2-9, closed. `docs/failure-modes.md`

Twelve failure modes, each in the same shape: symptom, mechanism, *where it is enforced*, and
residual risk. The discipline of the enforcement-point column is the point — a mitigation that
cannot name its file is a hope. R2-4 (spelled-out numbers) appears there as what it is: measured,
documented, not closed.

## Standing count after eight rounds

Thirty-four findings. Thirty-three fixed, one open and named: spelled-out-number grounding
(R2-4), which remains a measured, documented bound of the grounding gate rather than a defect
with a fix pending. The two unrun methods are unchanged — the literal container, and a live
model behind `make eval-live`.
