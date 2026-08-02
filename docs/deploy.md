# Production deployment

Campaign Copilot is deployed in Google Cloud project
`project-7a19790b-edfc-40dc-8f0`, region `us-central1`.

| Component | Production value |
|---|---|
| API | `https://cc-api-twkc6bjqaa-uc.a.run.app` |
| Runtime | Cloud Run, maximum one instance, scale to zero |
| Model | Vertex AI Gemini `gemini-3.5-flash` |
| Images | Artifact Registry, immutable Git commit tags |
| Secrets | Secret Manager, resource level access for the API identity only |
| Monitoring | readiness uptime check, error and readiness alerts, production dashboard |
| Notifications | `kazmirfahrier1989@gmail.com` |

The current verification record and deployed Git release are in the final section of this
document. The API is public at the Cloud Run edge so clients can reach it, but every `/v1/*`
route requires the application bearer token. `/health` and `/ready` intentionally expose no
secret data.

## Architecture

```text
client
  |
  | HTTPS and bearer token
  v
Cloud Run API  ---- signed HTTPS request ---->  Cloud Run executor
  |                                            |
  | Vertex AI through workload identity       | no project roles
  | Secret Manager access                     | no secrets
  | Cloud NAT egress                           | no Cloud NAT egress
  v                                            v
Gemini                                      isolated Python process
```

The executor has no useful project authorization and receives no application secret. Its
service URL can be routed by Cloud Run, but `/exec` and `/reset` require an Ed25519 signature
from the API. The signed message binds a timestamp, a random nonce, the HTTP method, path, and
SHA256 body digest. The executor rejects invalid signatures, requests older than 60 seconds,
and replayed nonces. The private key is available only to the API through Secret Manager. The
executor receives only the public key.

This application control is paired with network containment. The executor routes all outbound
traffic through a subnet with no Cloud NAT. Its dedicated service account has no project role
bindings. `assert_no_secrets()` also refuses to start the executor if a credential is visible
in its environment.

## Capacity contract

Conversation memory and Python namespaces are local to an instance. Terraform therefore caps
both services at one instance and permits scale to zero. A restart can lose session state.
Horizontal scaling is blocked until a shared session store and distributed turn serialization
exist.

The demo warehouse is built deterministically into the API image. A real customer deployment
would replace it with a managed warehouse connection. This release proves the governed query,
model, grounding, isolation, and operations path; it does not claim durable customer data or
multi region availability.

## Release process

Use the Git commit as the image tag. Never deploy `latest`.

```bash
PROJECT=project-7a19790b-edfc-40dc-8f0
REGION=us-central1
TAG=$(git rev-parse --short HEAD)

gcloud builds submit . \
  --project="${PROJECT}" \
  --config=cloudbuild.yaml \
  --substitutions="_IMAGE_TAG=${TAG}"

terraform -chdir=deploy/terraform init \
  -backend-config="bucket=${PROJECT}-cc-tf"
terraform -chdir=deploy/terraform plan \
  -var="project_id=${PROJECT}" \
  -var="image_tag=${TAG}" \
  -var="alert_email=kazmirfahrier1989@gmail.com" \
  -out=/tmp/campaign-copilot.tfplan
terraform -chdir=deploy/terraform apply /tmp/campaign-copilot.tfplan
```

Write secrets without a newline. A newline changes the bearer credential and will make every
otherwise correct client fail authentication.

```bash
API_BEARER_TOKEN=$(openssl rand -hex 32)
printf %s "${API_BEARER_TOKEN}" | \
  gcloud secrets versions add api-bearer-token \
  --project="${PROJECT}" --data-file=-
```

Rotating an environment variable secret requires a new Cloud Run revision so new instances
resolve the latest secret version.

## Production checks

Cloud Run reserves some request paths ending in `z`. Use `/health` and `/ready` for service
probes and external checks. The application retains `/healthz` and `/readyz` only as local
compatibility aliases.

```bash
API_URL=https://cc-api-twkc6bjqaa-uc.a.run.app
EXECUTOR_URL=https://cc-executor-twkc6bjqaa-uc.a.run.app
API_BEARER_TOKEN=$(gcloud secrets versions access latest \
  --secret=api-bearer-token --project="${PROJECT}")

curl -f "${API_URL}/health"
curl -f "${API_URL}/ready"
curl -f "${API_URL}/v1/info" \
  -H "Authorization: Bearer ${API_BEARER_TOKEN}"
curl -f "${EXECUTOR_URL}/health"
```

Also verify that anonymous `/v1/info` and unsigned executor `/exec` requests return 401, both
services are Ready, each routes 100 percent to its latest revision, and an authenticated chat
ends with a grounded `done` event.

## Verified deployment

The production deployment was exercised on 2026-08-02 with the real Cloud Run services,
Secret Manager, Vertex AI, and monitoring resources. The final release id and live evaluation
result are updated with each verified release. See [`runbook.md`](runbook.md) for incident
response and rollback.

Verified release: `f556190`.

| Check | Result |
|---|---|
| API and executor | Ready, latest revision equals latest created revision, 100 percent traffic |
| Public probes | `/health` 200 and `/ready` 200 with warehouse and executor both `ok` |
| API authentication | anonymous `/v1/info` 401; bearer authenticated `/v1/info` 200 |
| Executor authentication | unsigned `/exec` 401; signed API request 200 |
| Governed live query | Q4 spend `415685.23 USD`, grounding repaired once, final `done.ok=true` |
| Signed Python query | `python_exec` returned `42`, grounding passed, final `done.ok=true` |
| Model evaluation | 2 golden and 1 adversarial case; execution accuracy 1.0, grounding 1.0, injection block rate 1.0 |
| Monitoring | uptime check, readiness alert, error alert, dashboard, enabled email channel |
| Build and CI | Cloud Build success; GitHub Actions success for the preceding production change, final run linked from the PR |

The bounded live evaluation is preserved at
[`evals/live/20260802-f556190.json`](../evals/live/20260802-f556190.json). It also reports
`tool_call_f1=0.5333` and `schema_validity_rate=0.5`. Those values are evidence that the sample
is real, not a claim that three cases characterize the model. The deterministic policy suite
remains the stable CI regression gate; larger live samples belong in a scheduled evaluation
with an explicit budget.
