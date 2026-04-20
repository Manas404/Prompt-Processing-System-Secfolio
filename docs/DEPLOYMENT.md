# Deployment Guide

## Local Development (Docker Compose)

```bash
make env       # create .env from template
# add API keys to .env
make up        # start everything
make test      # run test suite
make logs      # tail logs
```

Access points:
- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- Flower: http://localhost:5555

---

## Production — Docker + Nginx

```nginx
# /etc/nginx/sites-available/pps
upstream pps_api {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://pps_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }
}
```

```bash
# On your server
git clone https://github.com/yourusername/prompt-processing-system.git
cd prompt-processing-system
cp .env.example .env && nano .env   # add real keys
docker-compose -f docker-compose.yml up -d
```

---

## Production — AWS ECS (Fargate)

### Services to create:

| Service | Task Definition | Replicas |
|---|---|---|
| `pps-api` | `CMD uvicorn app.main:app --host 0.0.0.0 --port 8000` | 2+ |
| `pps-worker` | `CMD celery -A app.tasks.celery_app worker --concurrency=8` | 2+ |
| `pps-beat` | `CMD celery -A app.tasks.celery_app beat` | 1 |

### Managed services:
- **Database:** AWS RDS PostgreSQL 16 with pgvector extension
- **Cache/Queue:** AWS ElastiCache Redis 7

### Environment variables (via Secrets Manager):
```
DATABASE_URL=postgresql://user:pass@rds-endpoint:5432/promptdb
REDIS_URL=redis://elasticache-endpoint:6379/0
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

---

## Production — Kubernetes

```yaml
# api-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pps-api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: pps-api
  template:
    metadata:
      labels:
        app: pps-api
    spec:
      containers:
      - name: api
        image: ghcr.io/yourusername/prompt-processing-system:latest
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: pps-secrets
        readinessProbe:
          httpGet:
            path: /api/v1/health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pps-worker
spec:
  replicas: 4
  template:
    spec:
      containers:
      - name: worker
        image: ghcr.io/yourusername/prompt-processing-system:latest
        command: ["celery", "-A", "app.tasks.celery_app", "worker",
                  "--loglevel=info", "--concurrency=8"]
        envFrom:
        - secretRef:
            name: pps-secrets
```

Scale workers:
```bash
kubectl scale deployment pps-worker --replicas=8
```

---

## Monitoring

### Health endpoint
```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","database":"ok","redis":"ok","celery":"ok",...}
```

### Celery Flower
Open http://localhost:5555 for real-time task monitoring, worker status, and failure rates.

### Recommended alerts
- `status != "ok"` on `/api/v1/health` → PagerDuty
- Queue depth > 1000 → scale workers
- Cache hit rate < 50% → investigate embedding quality
- Rate limit utilization > 90% → request provider quota increase
