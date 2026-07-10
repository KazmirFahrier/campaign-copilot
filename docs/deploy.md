# Deployment

**Nothing here has been applied.** No `terraform apply` has run against a real project, no
image has been built, and there is no live URL. What follows is verified to the extent stated
against each item, and no further. A README claiming a deployment that does not exist is the
same species of lie as an ungrounded number, and this project is about not telling those.

| Artifact | Verified how | Not verified |
|---|---|---|
| `deploy/terraform/*.tf` | `terraform fmt -check`, `terraform validate` against the real `hashicorp/google` v6 provider schema | never applied; no plan against a project. **`validate` checks syntax and provider schema. It says nothing about whether the two services can talk** — it passed for weeks while the api sent no auth token to an executor that requires one (`docs/AUDIT.md`, P0-2). |
| `deploy/Dockerfile.api`, `.executor` | reviewed; the wheel they install is built in CI, installed into a venv with no source tree, and both service factories are started (`package` job) | never built as images (no Docker in this environment) |
| `deploy/docker-compose.yml` | reviewed | never run |
| `web/` | `tsc --noEmit` under `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`; 7 unit tests via `node --test` | never served |
| `service/app.py`, `service/executor.py` | 24 tests, including api↔executor over an in-process ASGI transport | never run under uvicorn in production |

## The architecture, and why it is two services

```
        ┌───────────────┐      HTTP       ┌────────────────────┐
        │  api          │ ──────────────► │  executor          │
        │  holds keys   │                 │  holds nothing     │
        │  has egress   │                 │  no egress         │
        │  no untrusted │                 │  runs untrusted    │
        │  code         │                 │  Python            │
        └───────────────┘                 └────────────────────┘
```

Since Phase 2, `docs/threat-model.md` has said that `python_exec` is a resource limiter and
not a security boundary: it runs as the same UID as the agent, so anything executing inside it
can read `/proc/self/environ` and therefore the LLM API key. The document's answer was "the
real boundary is the container in Phase 6."

This is that container, and the boundary is enforced in three independent places, because a
property enforced in one place is a property that silently disappears:

1. **IAM.** The executor's service account has no role bindings at all. The Secret Manager
   accessor binding names the api's service account and only that one.
2. **Network.** The executor routes all egress through a subnet with no Cloud NAT and no
   default route. It can be called; it can call nothing. Its Cloud Run ingress is
   `INTERNAL_ONLY`, so the public internet cannot POST arbitrary Python to it.
3. **The application.** `assert_no_secrets()` runs at startup and raises if any credential is
   visible in the environment. A misconfigured revision that mounts the api's secrets into the
   executor **crash-loops** rather than quietly becoming insecure. Infrastructure rots; an
   assertion does not. Tested: `test_the_executor_refuses_to_start_if_it_can_see_a_credential`.

The seam is invisible to the agent. `RemoteSandbox.spec is PythonSandbox.spec` — literally the
same object — and both return the same `ToolResult`. There is a test asserting it. Where the
boundary sits is a deployment decision, and it must not change a line of the loop.

## What is deliberately not solved

- **The warehouse is baked into the image.** Fine for a demo with a deterministic generator;
  wrong for anything real. Production points `CC_WAREHOUSE` at BigQuery, which is a dbt profile
  change, not a rewrite.
- **Sessions are in-process.** `SessionStore` is a bounded LRU dict, so multi-turn works on
  **one instance** and breaks the moment Cloud Run autoscales past one. The fix is the Postgres
  session store the memory module was designed for and does not yet have.

  An earlier version of this file described exactly that limitation while the code had no
  session store at all: memory was constructed per request and discarded, so `session_id` was
  accepted, validated, threaded through, and dropped (`docs/AUDIT.md`, P0-3). Understating a
  total absence as a scaling caveat is worse than omitting it — it tells the reader the feature
  exists.
- **The executor's namespace is on local disk.** It does not survive an instance restart, and
  a session pinned to one instance is a session that vanishes. Same fix.
- **The `package` CI job exists because of R4-1.** Every data path used to be resolved relative
  to the repository, which is the same place as the package only under `pip install -e`. The
  containers install non-editable, so `SemanticLayer.load()` raised `FileNotFoundError` at
  startup and both images would have crash-looped. CI now builds the wheel, installs it where no
  checkout exists, and starts both factories.

- **No authentication on `/v1/chat`.** The api is `INGRESS_TRAFFIC_ALL` with no IAM invoker
  restriction. Do not put a key behind this without adding one.

- **Pinned facts are unreachable.** `ConversationMemory.pin()` exists, is tested, and has no
  caller: the agent has no tool with which to pin one (`docs/AUDIT.md`, P1-6). Adding one
  changes the agent's action space and needs eval cases before it changes the loop, so it is
  open rather than patched.

## Running it locally

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose -f deploy/docker-compose.yml up --build
curl -N localhost:8080/v1/chat -H 'content-type: application/json' \
  -d '{"question":"ROAS by channel last week"}'
```

The executor sits on an `internal: true` network: reachable from the api, with no route to the
internet. `network_mode: none` would be wrong — it removes ingress too, and the executor needs
to be callable. Ingress without egress is the property, and it is the same one the Terraform
encodes with a VPC connector and no NAT.

## Deploying

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
TAG=$(git rev-parse --short HEAD)     # never `latest`: a rollback must name a build
docker build -f deploy/Dockerfile.api      -t "${REPO}/api:${TAG}"      .
docker build -f deploy/Dockerfile.executor -t "${REPO}/executor:${TAG}" .
docker push "${REPO}/api:${TAG}" && docker push "${REPO}/executor:${TAG}"

cd deploy/terraform
terraform init
terraform plan  -var project_id="${PROJECT}" -var image_tag="${TAG}"
terraform apply -var project_id="${PROJECT}" -var image_tag="${TAG}"
```

The api needs `CC_EXECUTOR_AUDIENCE` set to the executor's URL so it can mint the OIDC identity
token Cloud Run's invoker binding requires. Without it every `python_exec` call returns 403.

Then, before trusting it:

```bash
curl -f "$(terraform output -raw api_url)/readyz"     # 503 until the warehouse answers
curl -f "$(terraform output -raw api_url)/metrics"     # ungrounded_blocked must be able to move
```

`ungrounded_blocked` sitting at zero forever is not good news. It means either that nobody has
asked a hard question, or that somebody switched the grounding gate off.
