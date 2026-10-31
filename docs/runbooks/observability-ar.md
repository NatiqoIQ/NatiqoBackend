# Sentry وOpenTelemetry — دليل التشغيل (F6)

`/metrics` موجود ويغطي المعدلات والكمون وحالة الـpool والقاطع والـreplica lag. هذا
الدليل يغطي الطبقة الثانية: **أثر واحد يعبر الطلب** (trace) و**استثناء واحد بتفاصيله**
(Sentry). الربط في `app/core/telemetry.py` ويُستدعى مرة واحدة من `create_app`.

## 1) المتطلبات

الـSDKs اختيارية ولم تُضف إلى `uv.lock` عمداً: نشر بلا مراقبة يجب ألا يدفع ثمن حزمتين.
ثبّتها في صورة الإنتاج بأحد الخيارين:

```bash
# Sentry فقط
pip install "sentry-sdk[fastapi]>=2.19"

# OTel كاملاً (exporter + الطبقات التي تغطيها الخطة)
pip install \
  "opentelemetry-sdk>=1.29" \
  "opentelemetry-exporter-otlp>=1.29" \
  "opentelemetry-instrumentation-fastapi>=0.50b0" \
  "opentelemetry-instrumentation-sqlalchemy>=0.50b0" \
  "opentelemetry-instrumentation-redis>=0.50b0" \
  "opentelemetry-instrumentation-httpx>=0.50b0"
```

إن ضُبط `SENTRY_DSN` أو `OTEL_ENABLED` دون الحزمة، يفشل الإقلاع برسالة واضحة بدل
التشغيل أعمى.

## 2) الإعداد

```bash
# Sentry
SENTRY_DSN=https://<key>@o<org>.ingest.sentry.io/<project>
SENTRY_TRACES_SAMPLE_RATE=0.05

# OTel
OTEL_ENABLED=true
OTEL_SERVICE_NAME=natiqo-api
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability:4317
OTEL_TRACES_SAMPLE_RATE=0.1
```

- `send_default_pii=False` مثبّت في الكود: لا تُرسَل أجسام الطلبات ولا نصوص المتعلم أو
  prompts الـAI إلى Sentry. الاستثناءات تحمل الـstack والـtags فقط.
- العينة `ParentBased`، فالتتبع يظل متصلاً بين الخدمات بدل أن يسقط عند الحدود.
- `/health` و`/metrics` مستثنيان من التتبع في `FastAPIInstrumentor`.

## 3) التحقق

1. شغّل API مع `OTEL_ENABLED=true` واتصل بالـcollector.
2. اطلب `GET /api/v1/content/tree` ثم `POST /api/v1/auth/google` (أو أي مسار).
3. في Tempo/Jaeger ابحث عن trace يبدأ من ingress، ويحتوي spans:
   `FastAPI` → `SQLAlchemy` (استعلامات الكتالوج) → `redis` (الـcache) → `httpx` إن
   استُدعي مزوّد خارجي. **معيار F6:** trace واحد من الطلب إلى SQL.
4. في Sentry، افتح خطأً مقصوداً (بيئة staging) وتأكد من وصول environment والـrelease.

## 4) التنبيهات

- نسبة `5xx` من Sentry > 1% خلال 5 دقائق.
- p95 من traces > 400ms (يطابق عتبة الـcanary).
- غياب traces من خدمة لمدة 10 دقائق أثناء وجود حركة = انقطاع في التتبع نفسه.
- `/metrics` يجب أن يبقى يعمل حتى لو تعطل الـcollector أو Sentry؛ فشلهما لا يوقف الطلبات.

## 5) ما لا يغطيه هذا المستند

- تشغيل الـcollector وTempo/Jaeger وSentry نفسها (بنية تحتية خارجية).
- Dashboards الثلاثة و8 تنبيهات Prometheus: موجودة في `deploy/monitoring/`.
- Tail sampling (100% للأخطاء وp99) يحتاج إعداد الـcollector، لا الكود.
