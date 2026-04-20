# ARK on EKS Implementation Guide

This guide explains how to set up the Kubernetes (EKS) compute plane for the ARK platform.

## Architecture

The ARK Webapp launches research jobs as Kubernetes `Job` objects in the `ark-jobs` namespace. Since the webapp and the jobs may run in different environments (e.g., local webapp vs. cloud cluster), project data is transferred via an **S3 Storage Bridge**.

## Prerequisites

1.  **S3 Bucket**: A bucket where job data will be staged.
2.  **IAM Role (IRSA)**: An IAM Role with read/write access to the S3 bucket, associated with the `ark-job-sa` ServiceAccount in the `ark-jobs` namespace.
3.  **Docker Registry**: A registry (e.g., ECR) to host the `ark-job` image.

## Setup Steps

### 1. Provision Infrastructure

Apply the manifests:

```bash
kubectl apply -f ark-jobs-namespace.yaml
kubectl apply -f ark-rbac.yaml
kubectl apply -f ark-limitrange.yaml
```

### 2. Configure IAM (IRSA)

Create an IAM policy that allows access to your S3 bucket:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject"],
            "Resource": [
                "arn:aws:s3:::YOUR_BUCKET_NAME",
                "arn:aws:s3:::YOUR_BUCKET_NAME/*"
            ]
        }
    ]
}
```

Associate this policy with an IAM Role and link it to the ServiceAccount:

```bash
eksctl create iamserviceaccount \
    --name ark-job-sa \
    --namespace ark-jobs \
    --cluster YOUR_CLUSTER_NAME \
    --attach-policy-arn arn:aws:iam::ACCOUNT_ID:policy/YOUR_POLICY_NAME \
    --approve \
    --override-existing-serviceaccounts
```

### 3. Build and Push Job Image

```bash
# From project root
docker build -t YOUR_REGISTRY/ark-job:latest -f docker/Dockerfile.job .
docker push YOUR_REGISTRY/ark-job:latest
```

### 4. Configure Webapp

Update your `.ark/webapp.env` or environment variables:

```env
K8S_ENABLED=true
K8S_JOB_IMAGE=YOUR_REGISTRY/ark-job:latest
K8S_S3_BUCKET=YOUR_BUCKET_NAME
K8S_S3_REGION=us-east-1
```

## Troubleshooting

- **Job stuck in Pending**: Check node resources and `ark-jobs-limits`. Ensure nodes have the requested CPU/RAM.
- **S3 Download Fails**: Check IRSA configuration. Run `kubectl describe pod` to verify the `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` environment variables are injected.
- **PDFs Not Found**: Check job logs (`kubectl logs -l app=ark-job`) to see if the orchestrator finished and uploaded results correctly.
