# campaign-copilot on Cloud Run.
#
# Two services, because the security boundary is a deployment decision:
#
#   api       uses Google workload identity, has egress, runs no untrusted code
#   executor  holds nothing, has no egress, runs model-authored Python
#
# The properties that matter are enforced in three places, deliberately:
#
#   1. Here, in IAM: the executor's service account has no role bindings at all. The api
#      receives only Vertex AI access and its application bearer secret.
#   2. In the network: the executor routes all egress through a subnet with no Cloud NAT,
#      so it has no path to the internet.
#   3. In the application: assert_no_secrets() crash-loops the executor if a credential is
#      ever visible in its environment. Infrastructure rots; an assertion does not.
#
# State lives in a versioned GCS bucket supplied at `terraform init` time. The bucket is the
# only bootstrap resource created outside Terraform.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    prefix = "campaign-copilot"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  description = "Cloud Run region."
  default     = "us-central1"
}

variable "image_tag" {
  type        = string
  description = "Immutable image tag. Never `latest`: a rollback must name a build."
}

variable "alert_email" {
  type        = string
  description = "Email address that receives production alert notifications."
}

locals {
  repo           = "${var.region}-docker.pkg.dev/${var.project_id}/campaign-copilot"
  api_image      = "${local.repo}/api:${var.image_tag}"
  executor_image = "${local.repo}/executor:${var.image_tag}"
}

resource "google_project_service" "required" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "campaign-copilot"
  format        = "DOCKER"
  depends_on    = [google_project_service.required]
}

# ---------------------------------------------------------------- identities

resource "google_service_account" "api" {
  account_id   = "cc-api"
  display_name = "campaign-copilot api"
}

resource "google_project_iam_member" "api_uses_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# Deliberately bound to nothing. If a future change grants this account a role, that diff
# should be the loudest line in the pull request.
resource "google_service_account" "executor" {
  account_id   = "cc-executor"
  display_name = "campaign-copilot executor (holds nothing)"
}

# ------------------------------------------------------------------- secrets

resource "google_secret_manager_secret" "api_bearer_token" {
  secret_id = "api-bearer-token"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "api_reads_bearer_token" {
  secret_id = google_secret_manager_secret.api_bearer_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# ------------------------------------------------------------------- network

resource "google_compute_network" "vpc" {
  name                    = "cc-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

# Both services use Direct VPC egress. Only the api subnet is attached to Cloud NAT. The
# executor subnet has no external addresses and is deliberately excluded from NAT, so its
# default route has no next hop that can carry its traffic to the internet.
resource "google_compute_subnetwork" "no_egress" {
  name          = "cc-no-egress"
  ip_cidr_range = "10.8.0.0/26"
  region        = var.region
  network       = google_compute_network.vpc.id
}

resource "google_compute_subnetwork" "api" {
  name                     = "cc-api-egress"
  ip_cidr_range            = "10.8.1.0/26"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true
}

# Cloud NAT would make calls to the executor's public run.app address arrive from an external
# source, which internal ingress correctly rejects. Resolve run.app through Google's private
# VIPs so service to service traffic stays on the VPC and is classified as internal.
resource "google_dns_managed_zone" "run_app_private" {
  name        = "cc-run-app-private"
  dns_name    = "run.app."
  description = "Private Google Access routing for internal Cloud Run calls."
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.id
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_dns_record_set" "run_app_private_vip" {
  managed_zone = google_dns_managed_zone.run_app_private.name
  name         = google_dns_managed_zone.run_app_private.dns_name
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"]
}

resource "google_dns_record_set" "run_app_wildcard" {
  managed_zone = google_dns_managed_zone.run_app_private.name
  name         = "*.${google_dns_managed_zone.run_app_private.dns_name}"
  type         = "CNAME"
  ttl          = 300
  rrdatas      = [google_dns_managed_zone.run_app_private.dns_name]
}

resource "google_compute_router" "api" {
  name    = "cc-api-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "api" {
  name                               = "cc-api-nat"
  router                             = google_compute_router.api.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.api.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

# ------------------------------------------------------------------ services

resource "google_cloud_run_v2_service" "executor" {
  name     = "cc-executor"
  location = var.region

  # Only reachable from inside the project. The public internet cannot POST arbitrary Python.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account                  = google_service_account.executor.email
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.no_egress.id
      }
    }

    containers {
      image = local.executor_image

      # No env block. No secrets. assert_no_secrets() enforces it at startup.
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        failure_threshold = 3
        period_seconds    = 5
      }
    }
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "cc-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.api.email
    max_instance_request_concurrency = 16
    labels = {
      release        = var.image_tag
      network_config = "private-dns-v2"
    }

    # Conversation memory is intentionally in-process. One instance makes that contract
    # correct and explicit. Horizontal scale requires a shared session backend first.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.api.id
      }
    }

    containers {
      image = local.api_image

      env {
        name  = "CC_EXECUTOR_URL"
        value = google_cloud_run_v2_service.executor.uri
      }

      env {
        name  = "CC_EXECUTOR_AUDIENCE"
        value = google_cloud_run_v2_service.executor.uri
      }

      env {
        name  = "CC_ENVIRONMENT"
        value = "production"
      }

      env {
        name  = "CC_RELEASE"
        value = var.image_tag
      }

      env {
        name  = "CC_LLM_PROVIDER"
        value = "gemini"
      }

      env {
        name  = "CC_MODEL"
        value = "gemini-3.5-flash"
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name = "CC_API_BEARER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.api_bearer_token.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      # Liveness must not depend on the warehouse; readiness must. A dependency outage
      # should drain a revision, not restart it in a loop.
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }

      startup_probe {
        http_get {
          path = "/readyz"
        }
        failure_threshold = 6
        period_seconds    = 5
      }
    }
  }

  depends_on = [
    google_compute_router_nat.api,
    google_dns_record_set.run_app_private_vip,
    google_dns_record_set.run_app_wildcard,
    google_project_iam_member.api_uses_vertex,
    google_secret_manager_secret_iam_member.api_reads_bearer_token,
    google_cloud_run_v2_service_iam_member.api_invokes_executor,
  ]
}

# The api is the only identity permitted to call the executor.
resource "google_cloud_run_v2_service_iam_member" "api_invokes_executor" {
  name     = google_cloud_run_v2_service.executor.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

# Cloud Run accepts public traffic at the edge, then the application bearer token protects every
# `/v1/` route. Health and readiness remain available to Cloud Monitoring without a shared key.
resource "google_cloud_run_v2_service_iam_member" "public_invokes_api" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------- monitoring

resource "google_monitoring_notification_channel" "email" {
  display_name = "Campaign Copilot production email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  force_delete = false
  depends_on   = [google_project_service.required]
}

resource "google_logging_metric" "api_errors" {
  name        = "campaign_copilot_api_errors"
  description = "Error severity log entries emitted by the Campaign Copilot API."
  filter      = "resource.type=\"cloud_run_revision\" resource.labels.service_name=\"cc-api\" severity>=ERROR"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.required]
}

resource "google_monitoring_uptime_check_config" "api_ready" {
  display_name = "Campaign Copilot readiness"
  timeout      = "10s"
  period       = "300s"

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
    }
  }

  http_check {
    path         = "/readyz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  content_matchers {
    content = "\"ready\":true"
    matcher = "CONTAINS_STRING"
  }
}

resource "google_monitoring_alert_policy" "readiness" {
  display_name = "Campaign Copilot readiness failed"
  combiner     = "OR"

  conditions {
    display_name = "Readiness check is failing"
    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id = \"${google_monitoring_uptime_check_config.api_ready.uptime_check_id}\""
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "120s"

      aggregations {
        alignment_period   = "120s"
        per_series_aligner = "ALIGN_NEXT_OLDER"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]
  documentation {
    content   = "Campaign Copilot readiness failed. Follow docs/runbook.md and inspect both Cloud Run revisions before rollback."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "api_errors" {
  display_name = "Campaign Copilot API errors"
  combiner     = "OR"

  conditions {
    display_name = "At least one error log in five minutes"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"cc-api\" AND severity>=ERROR"
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]
  documentation {
    content   = "Campaign Copilot emitted an error log. Correlate the request id in Cloud Logging and use docs/runbook.md."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_dashboard" "production" {
  dashboard_json = jsonencode({
    displayName = "Campaign Copilot production"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width  = 6
          height = 4
          widget = {
            title = "API request count by response code"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"cc-api\" AND metric.type=\"run.googleapis.com/request_count\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.response_code_class"]
                    }
                  }
                }
              }]
              yAxis = { label = "requests per second", scale = "LINEAR" }
            }
          }
        },
        {
          xPos   = 6
          width  = 6
          height = 4
          widget = {
            title = "API request latency"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"cc-api\" AND metric.type=\"run.googleapis.com/request_latencies\""
                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_PERCENTILE_95"
                    }
                  }
                }
              }]
              yAxis = { label = "latency", scale = "LINEAR" }
            }
          }
        },
        {
          yPos   = 4
          width  = 12
          height = 3
          widget = {
            title = "Readiness"
            scorecard = {
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "resource.type=\"uptime_url\" AND metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\""
                  aggregation = {
                    alignmentPeriod  = "60s"
                    perSeriesAligner = "ALIGN_NEXT_OLDER"
                  }
                }
              }
              thresholds = [{ value = 1, color = "RED", direction = "BELOW" }]
            }
          }
        }
      ]
    }
  })
  depends_on = [google_project_service.required]
}

output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Public edge URL. Application bearer authentication protects /v1 routes."
}

output "executor_url" {
  value       = google_cloud_run_v2_service.executor.uri
  description = "Internal-only URL of the executor."
}

output "dashboard_id" {
  value       = google_monitoring_dashboard.production.id
  description = "Cloud Monitoring dashboard resource id."
}
