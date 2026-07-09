#!/usr/bin/env bash
#
# setup_ark_launcher_aws.sh — create + activate the central "ARK launcher" AWS
# identity that SkyPilot uses to provision compute. The AWS analog of
# setup_ark_launcher_sa.sh.
#
# In the workspaces model (see SKYPILOT_PLAN.md), ONE central identity provisions
# into every user's account. AWS has no cross-project grant like GCP — isolation
# is per ACCOUNT, reached by cross-account STS AssumeRole. So each user creates an
# "ark-launcher" ROLE in their own account whose trust policy names THIS identity
# (no key material ever leaves their account); a per-launch profile just assumes
# that role to target the user's account.
#
# This script creates that central identity (an IAM user with access keys) on the
# operator's own account, grants it permission to assume any tenant "ark-launcher"
# role, and writes a base ~/.aws profile so the webapp host's SkyPilot server can
# assume tenant roles as it. It prints the identity ARN to put in webapp.env as
# CLOUD_LAUNCHER_ROLE_ARN (users' trust policies name this ARN).
#
# Idempotent: safe to re-run. It will NOT mint a second access key if a base
# profile already exists locally.
#
# Alternative to an IAM user: if the webapp host runs on EC2, you can skip this
# script's key creation and instead set CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE=
# Ec2InstanceMetadata (the host's instance role becomes the launcher identity);
# still attach the assume-role permission below to that role.
#
# Usage:
#   scripts/setup_ark_launcher_aws.sh
#   Uses your current AWS CLI credentials (must be able to create IAM users).

set -euo pipefail

USER_NAME="ark-launcher"
PROFILE="ark-launcher"                 # base ~/.aws profile the webapp assumes tenant roles via
TENANT_ROLE_NAME="ark-launcher"        # the role users create in THEIR account (matches aws_access.py)
AWS_DIR="${HOME}/.aws"
ENV_FILE="${HOME}/.config/ark/launcher-aws.env"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found — install it first." >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ -z "${ACCOUNT_ID}" || "${ACCOUNT_ID}" == "None" ]]; then
  echo "ERROR: could not resolve your AWS account (configure the aws CLI first)." >&2
  exit 1
fi
log "Operator account: ${ACCOUNT_ID}"

# Policy that lets the launcher assume ANY tenant's ark-launcher role. Scoped to
# the fixed role name across all accounts (resource = role/ark-launcher in *) so
# no per-tenant policy edit is needed as users onboard.
ASSUME_POLICY_NAME="ark-launcher-assume-tenant-roles"
ASSUME_POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::*:role/${TENANT_ROLE_NAME}"
  }]
}
EOF
)

# 1. Create the launcher IAM user (idempotent).
if aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1; then
  log "IAM user '${USER_NAME}' already exists — skipping create."
else
  log "Creating IAM user '${USER_NAME}'..."
  aws iam create-user --user-name "${USER_NAME}" >/dev/null
fi
USER_ARN="$(aws iam get-user --user-name "${USER_NAME}" --query User.Arn --output text)"

# 2. Attach the assume-tenant-roles permission (put-user-policy is idempotent).
log "Attaching inline policy '${ASSUME_POLICY_NAME}'..."
aws iam put-user-policy \
  --user-name "${USER_NAME}" \
  --policy-name "${ASSUME_POLICY_NAME}" \
  --policy-document "${ASSUME_POLICY_DOC}" >/dev/null

# 3. Create access keys + a base ~/.aws profile (only if we don't already hold
#    one locally — re-running must not accumulate orphaned keys on the user).
mkdir -p "${AWS_DIR}"
chmod 700 "${AWS_DIR}"
if aws configure get aws_access_key_id --profile "${PROFILE}" >/dev/null 2>&1; then
  log "Base profile '${PROFILE}' already configured — skipping key creation."
else
  log "Creating access key -> ~/.aws profile '${PROFILE}'"
  CREDS_JSON="$(aws iam create-access-key --user-name "${USER_NAME}" --output json)"
  AK="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')"
  SK="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')"
  aws configure set aws_access_key_id "${AK}" --profile "${PROFILE}"
  aws configure set aws_secret_access_key "${SK}" --profile "${PROFILE}"
  chmod 600 "${AWS_DIR}/credentials" 2>/dev/null || true
fi

# 4. Persist the launcher settings to a sourceable env file for the webapp host.
mkdir -p "$(dirname "${ENV_FILE}")"
cat > "${ENV_FILE}" <<EOF
# Add these to ~/.ark/webapp.env (or source before \`ark webapp\`) so SkyPilot
# AWS launches assume tenant roles as the ARK launcher.
CLOUD_LAUNCHER_ROLE_ARN=${USER_ARN}
CLOUD_LAUNCHER_AWS_PROFILE=${PROFILE}
# CLOUD_AWS_REGION=us-east-1
# CLOUD_LAUNCHER_AWS_EXTERNAL_ID=            # optional; set + redeploy to require it
EOF
chmod 600 "${ENV_FILE}"

log "Done."
echo
echo "  Launcher identity : ${USER_ARN}"
echo "  Base profile      : ${PROFILE}  (~/.aws/credentials)"
echo "  Env file          : ${ENV_FILE}"
echo
echo "  Put this in ~/.ark/webapp.env, then restart the webapp:"
echo "      CLOUD_LAUNCHER_ROLE_ARN=${USER_ARN}"
echo "      CLOUD_LAUNCHER_AWS_PROFILE=${PROFILE}"
echo
echo "  Each user then creates an '${TENANT_ROLE_NAME}' role in THEIR account that"
echo "  trusts ${USER_ARN} — the dashboard's AWS onboarding generates the exact"
echo "  create-role commands for them."
