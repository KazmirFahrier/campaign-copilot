# campaign-copilot on Cloud Run.
#
# Two services, because the security boundary is a deployment decision:
#
#   api       holds the LLM credential, has egress, runs no untrusted code
#   executor  holds nothing, has no egress, runs model-authored Python
#
# The properties that matter are enforced in three places, deliberately:
#
#   1. Here, in IAM: the executor's service account has no role bindings at all, and the
#      Secret Manager accessor binding names the api's service account only.
#   2. In the network: the executor routes all egress through a subnet with no Cloud NAT,
#      so it has no path to the internet.
#   3. In the application: assert_no_secrets() crash-loops the executor if a credential is
#      ever visible in its environment. Infrastructure rots; an assertion does not.
#
# NOT YET APPLIED. No `terraform apply` has been run against a real project. This
# configuration passes `terraform fmt -check` and `terraform validate`, which is a statement
# about its syntax and its provider schema, and about nothing else.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
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

locals {
  repo           = "${var.region}-docker.pkg.dev/${var.project_id}/campaign-copilot"
  api_image      = "${local.repo}/api:${var.image_tag}"
  executor_image = "${local.repo}/executor:${var.image_tag}"
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "campaign-copilot"
  format        = "DOCKER"
}

# ---------------------------------------------------------------- identities

resource "google_service_account" "api" {
  account_id   = "cc-api"
  display_name = "campaign-copilot api"
}

# Deliberately bound to nothing. If a future change grants this account a role, that diff
# should be the loudest line in the pull request.
resource "google_service_account" "executor" {
  account_id   = "cc-executor"
  display_name = "campaign-copilot executor (holds nothing)"
}

# ------------------------------------------------------------------- secrets

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "anthropic-api-key"
  replication {
    auto {}
  }
}

# The api may read the key. The executor is not named here, and must never be.
resource "google_secret_manager_secret_iam_member" "api_reads_key" {
  secret_id = google_secret_manager_secret.anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# ------------------------------------------------------------------- network

resource "google_compute_network" "vpc" {
  name                    = "cc-vpc"
  auto_create_subnetworks = false
}

# No Cloud NAT is attached to this subnet, and no route to 0.0.0.0/0 exists for it. Anything
# whose egress is routed here reaches the VPC and stops.
resource "google_compute_subnetwork" "no_egress" {
  name          = "cc-no-egress"
  ip_cidr_range = "10.8.0.0/28"
  region        = var.region
  network       = google_compute_network.vpc.id
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
    service_account = google_service_account.api.email

    containers {
      image = local.api_image

      env {
        name  = "CC_EXECUTOR_URL"
        value = google_cloud_run_v2_service.executor.uri
      }

      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_api_key.secret_id
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

  depends_on = [google_secret_manager_secret_iam_member.api_reads_key]
}

# The api is the only identity permitted to call the executor.
resource "google_cloud_run_v2_service_iam_member" "api_invokes_executor" {
  name     = google_cloud_run_v2_service.executor.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Public URL of the api service."
}

output "executor_url" {
  value       = google_cloud_run_v2_service.executor.uri
  description = "Internal-only URL of the executor."
}
