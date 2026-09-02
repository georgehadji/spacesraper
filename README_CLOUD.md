# ☁️ Spacescraper Cloud Deployment Guide

The Spacescraper cluster is containerized and ready for orchestration on any major cloud provider.

## 🧱 Deployment Options

### Option A: Cloud VM (AWS EC2, DigitalOcean, Azure VM)
This is the simplest method using the provided `deploy.sh`.
1.  Launch a Linux instance (Ubuntu 22.04 recommended).
2.  Install Docker and Docker Compose.
3.  Clone the repository.
4.  Run `./deploy.sh`.

### Option B: Managed Containers (AWS ECS / Fargate)
For enterprise scalability without managing VMs:
1.  **Registry**: Push the image to AWS ECR:
    ```bash
    docker build -t spacescraper .
    docker tag spacescraper:latest [AWS_ACCOUNT_ID].dkr.ecr.[REGION].amazonaws.com/spacescraper:latest
    docker push [AWS_ACCOUNT_ID].dkr.ecr.[REGION].amazonaws.com/spacescraper:latest
    ```
2.  **Task Definitions**: Create three tasks in ECS based on the `docker-compose.yml` service definitions (`api`, `scraper`, `processor`).
3.  **Auto-scaling**: Configure Service Auto-scaling on the `scraper` task based on CPU utilization or Custom CloudWatch metrics from Valkey.

## 🔐 Production Variables
Ensure these are set in your Cloud Provider's Secret Manager (AWS Secrets Manager / GCP Secret Manager):
- `GEMINI_API_KEY`: For AI Self-Healing.
- `VALKEY_URL`: If using a managed Valkey service (for example AWS ElastiCache for Valkey), replace the local URL.

## ⚡ Performance Tuning
- **Scraper Persistence**: Ensure the `exports/` folder is mapped to a persistent volume (AWS EFS) if running multiple ephemeral nodes.
- **Worker Concurrency**: Adjust `replicas` in `docker-compose.yml` to scale the crawling capacity.
