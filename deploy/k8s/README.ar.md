# تشغيل Natiqo على Kubernetes

هذه الملفات هي الجواب القابل للتنفيذ على **P2-5** (Autoscaling/Canary) و**P2-8** (الأسرار)
في `docs/system-design-scaling-plan-ar.md`. لم تُشغَّل داخل هذه البيئة (لا Docker ولا
cluster)، وكل رقم فيها يجب أن يُتحقق منه في staging قبل الإنتاج.

## الملفات

| الملف | ماذا يفعل | لماذا |
|---|---|---|
| `deployment.yaml` | 3 نسخ، `maxUnavailable: 0`، probes، PDB، `runAsNonRoot` | النشر المتدرّج لا يفقد سعة، والـreadiness لا يقتل نسخة بسبب Redis |
| `hpa.yaml` | 3–24 نسخة على CPU 60% وMemory 75% | توسّع أفقي سريع، وتقليص بطيء (5 دقائق) حتى لا تُفقد نسخة قبل موجة أخرى |
| `rollout.yaml` | Argo Rollouts: 5% → 25% → 100% مع تحليل Prometheus | canary حقيقي: 5% من الطلبات على النسخة الجديدة، والتراجع آلي عند تجاوز 1% أخطاء |
| `migrate-job.yaml` | `alembic upgrade head` مرة واحدة قبل النشر | الترحيل ليس مسؤولية كل pod عند الإقلاع (F3) |

## قبل أول نشر

1. **ابنِ صورة غير قابلة للتغيير** وثبّت الـdigest لا الوسم:
   ```bash
   docker build -t ghcr.io/natiqo/natiqo-backend:$(git rev-parse --short HEAD) .
   docker push ghcr.io/natiqo/natiqo-backend:$(git rev-parse --short HEAD)
   # ثم استبدل REPLACE_WITH_DIGEST بـ ghcr.io/natiqo/natiqo-backend@sha256:...
   ```
2. **الأسرار من Secret Manager**، لا من `.env` على الخادم:
   ```bash
   kubectl create secret generic natiqo-api-secrets \
     --from-literal=JWT_SECRET="$(openssl rand -base64 48)" \
     --from-literal=JWT_REFRESH_SECRET="$(openssl rand -base64 48)" \
     --from-literal=DATABASE_URL="postgresql+asyncpg://..." \
     --from-literal=DATABASE_READ_URL="postgresql+asyncpg://replica..." \
     --from-literal=REDIS_URL="redis://..." \
     --from-literal=QUEUE_REDIS_URL="redis://..."
   ```
   الأفضل: External Secrets Operator أو CSI driver يزامن من Vault/AWS Secrets Manager،
   فيبقى السر خارج Git وخارج `kubectl` history.
3. **PgBouncer**: اضبط `DATABASE_MAX_OVERFLOW` و`replicas` بحيث
   `replicas × (pool_size + max_overflow) ≤ max_db_connections × 0.8`. مع 24 نسخة و
   `10 + 5` هذا 360 اتصالاً — أكبر من غالبية قواعد البيانات المُدارة الصغيرة.
   اقرأ `deploy/pgbouncer/README.ar.md` قبل رفع `maxReplicas`.
4. **الترحيل**:
   ```bash
   kubectl delete job natiqo-migrate --ignore-not-found
   kubectl apply -f deploy/k8s/migrate-job.yaml
   kubectl wait --for=condition=complete job/natiqo-migrate --timeout=10m
   ```
   **بعد الترحيل، مرة واحدة فقط:** شغّل Backfill لجدول `daily_metrics` بنفس صورة
   التطبيق ونفس env (يمكن pod مؤقت أو `kubectl exec` في pod API):
   ```bash
   python -m scripts.backfill_daily_metrics --dry-run   # يطبع النطاق فقط
   python -m scripts.backfill_daily_metrics             # يكتب ثم يفعّل الـRollup
   ```
   قبل انتهائه تبقى لوحة الإدارة على الاستعلامات القديمة (صحيحة لكن أبطأ). إن أردت
   تأجيل التفعيل: `--no-mark`. أعد تشغيل نفس الأمر بعد أي انقطاع؛ كل دفعة idempotent.
5. **النشر**: `kubectl argo rollouts get rollout natiqo-api --watch`، ثم
   `kubectl argo rollouts promote natiqo-api` بعد كل خطوة إن لم تُضبط الخطوات الآلية.

## التراجع

```bash
kubectl argo rollouts abort natiqo-api        # يعيد 100% للنسخة المستقرة
kubectl argo rollouts undo natiqo-api         # للـrevision السابق
```
لا تُشغّل `alembic downgrade` تلقائياً مع التراجع: الترحيلات تتبع Expand/Contract،
والكود القديم يجب أن يعمل مع المخطط الجديد. إن كان الترحيل قد حذف عموداً، فالتراجع
قرار بشري لا خطوة آلية.

## الحدود المعروفة

- لا NetworkPolicy هنا؛ أضفها لتقييد الـegress إلى PostgreSQL/Redis/S3 فقط (P2-8).
- لا WAF/DDoS؛ يبقى عند مزوّد الحافة (P1-1).
- `readOnlyRootFilesystem` يحتاج `PYTHONDONTWRITEBYTECODE=1` وإلا ستحاول Python كتابة
  `__pycache__`؛ أضِفه إلى `env` إن لم يكن في الصورة.
- التحليل يفترض أن Prometheus يعرف الـnamespace وأن `/metrics` محمي من الإنترنت.
