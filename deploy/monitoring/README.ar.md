# مراقبة Natiqo — ما هو موجود وما هو ناقص

هذا المجلد يحوّل `/metrics` من endpoint إلى تشغيل فعلي: قواعد تنبيه، إعداد scrape، ولوحة Grafana جاهزة للاستيراد.

## ما تشحنه هذه الملفات

| الملف | الغرض |
|---|---|
| `prometheus.yml` | scrape كل 15 ثانية من `/metrics`، مع مثال Kubernetes SD معطّل |
| `alerts.yml` | 10 تنبيهات مبنية **فقط** على series تصدّرها التطبيق فعلاً |
| `dashboards/natiqo-overview.json` | لوحة واحدة بثلاثة أقسام: Traffic، Saturation، Learner-facing health |

## التنبيهات

| التنبيه | الشرط | الخطورة |
|---|---|---|
| `NatiqoInstanceDown` | `up == 0` لمدة دقيقتين | critical |
| `NatiqoNoTraffic` | صفر طلبات مع `up == 1` لعشر دقائق | critical |
| `NatiqoHighErrorRate` | نسبة 5xx > 1% لخمس دقائق | critical |
| `NatiqoSlowRequests` | p95 > 500ms | critical |
| `NatiqoReadLatencyBudget` | p95 لمسارات القراءة > 200ms | warning |
| `NatiqoDbPoolExhausted` | `checked_out >= pool_size` | critical |
| `NatiqoDbPoolSaturated` | إشغال pool > 70% لعشر دقائق | warning |
| `NatiqoQueueBacklog` | عمق الطابور > 1,000 لعشر دقائق | warning |
| `NatiqoTooManyRateLimits` | نسبة 429 > 5% لعشر دقائق | warning |
| `NatiqoTrafficAboveDesign` | > 2,000 RPS مستمرة | warning |

العتبات مضبوطة على هدف التصميم (2,000 RPS)، لا على حمل اليوم. راجعها بعد أول Baseline في `docs/system-design-scaling-plan-ar.md` §10.

## تشغيل محلي سريع

```bash
# 1) شغّل الـ API ثم تحقق من التعريض
curl -s localhost:8000/metrics | head -30

# 2) شغّل Prometheus مع القواعد
docker run --rm -p 9090:9090 \
  -v "$PWD/deploy/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/deploy/monitoring/alerts.yml:/etc/prometheus/rules/alerts.yml:ro" \
  prom/prometheus:latest

# 3) استورد اللوحة في Grafana: Dashboards -> Import -> Upload JSON
#    واختر datasource الـ Prometheus.
```

## ما لا يزال غير موصول (F6)

- **Sentry**: `SENTRY_DSN` موجود في الإعدادات ولا يقرأه أحد. إضافته تحتاج `sentry-sdk` إلى `pyproject.toml` + `uv.lock`.
- **OTel traces**: نفس الحالة لـ `OTEL_*`. غياب التتبّع يعني أن الربط بين طلب HTTP واستعلام SQL ونداء AI ما زال يدوياً عبر `requestId`.
- **لوحات إضافية**: اللوحة الحالية تغطي الثلاثة في ملف واحد؛ التقسيم إلى ثلاث لوحات منفصلة مسألة تنظيمية لا نقص في البيانات.

لا تُغلق F6 في تقرير الإنجاز قبل أن يوجد: DSN فعلي، trace واحد يظهر من الـ ingress إلى SQL، وتنبيه واحد على الأقل وصل إلى قناة on-call.
