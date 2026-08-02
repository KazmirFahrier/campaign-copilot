# Production runbook

This service is released as an immutable API image and an immutable executor image with the
same Git commit tag. Terraform pins each service to one warm instance because conversation and
Python namespace state are local. Do not raise either maximum instance count until a shared
session backend exists.

## Release gate

Before a release:

1. Run `make warehouse`, `make check`, `make eval-gate`, and `make report`.
2. Build both images with the Git commit as the tag. Never use `latest`.
3. Confirm secret versions exist for `anthropic-api-key` and `api-bearer-token`.
4. Run `terraform plan` and review every IAM, network, secret, scaling, and image change.
5. Apply, then verify `/healthz`, `/readyz`, authenticated `/v1/info`, and one grounded chat.
6. Confirm every streamed event reports the expected `release` and `model`.

## Service objectives

The initial objectives are deliberately conservative for the single instance profile:

| Signal | Objective | Page when |
|---|---:|---:|
| availability | 99.5 percent over 30 days | below 99 percent for 15 minutes |
| request errors | below 1 percent | above 5 percent for 5 minutes |
| p95 latency | below 30 seconds | above 45 seconds for 10 minutes |
| grounded answers shipped | exactly 100 percent | any ungrounded answer ships |
| evaluation gate | every row passes | any required metric is missing or regresses |

`ungrounded_blocked` is a safety action, not an error. Alert if it drops to zero for a full
week while answers continue, because that can indicate the gate is no longer exercising. Alert
on a sudden rise as a quality regression in the model, prompt, tool output, or data.

## Incident triage

Use the `X-Request-ID` from the response or SSE event to find the JSON log line. Record the
`release` and `model` carried by the same event before changing anything.

| Symptom | First checks | Action |
|---|---|---|
| readiness fails | warehouse and executor entries in `/readyz` | restore the dependency or roll back |
| executor unavailable | audience, invoker binding, identity token, internal ingress | repair identity or network configuration |
| authentication rejected | bearer secret version and caller header | rotate or restore the secret, never disable auth |
| overload responses | `overloaded`, p95 latency, Cloud Run concurrency | reduce traffic or optimize the slow dependency |
| grounding blocks rise | prompt fingerprint, model release, tool facts, SQL guard verdicts | roll back the model or prompt if the change caused it |
| tool failures rise | error codes by tool and executor health | repair the named dependency before changing prompts |

## Rollback

Rollback changes both images to the previous known good Git tag. Do not roll back only the API
or only the executor because their request contract is versioned together.

1. Set `image_tag` to the previous green commit.
2. Review `terraform plan` and confirm only the two image revisions change.
3. Apply and wait for `/readyz` to return success.
4. Run an authenticated grounded query and confirm its SSE `release` is the rollback tag.
5. Preserve the failed revision logs and evaluation artifacts for the post incident review.

## Known capacity boundary

This profile favors a correct state model over horizontal scale. It has bounded admission
through `CC_MAX_CONCURRENT_REQUESTS`, and excess work receives status 429 with `Retry-After`.
Conversation state can be lost on instance restart. That is a known product limitation, not a
silent availability guarantee. A multi instance release is blocked until session memory and
executor namespaces use a shared store with distributed turn serialization.
