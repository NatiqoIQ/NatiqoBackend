# Runbook — فشل Redis والتحوّل إلى نسخة أخرى

**النطاق:** Redis الكاش (`REDIS_URL`) وRedis الطابور (`QUEUE_REDIS_URL`) — وهما
مستقلان عمداً: الكاش يمكن أن يُطرد تحت الضغط، والطابور لا. فشل أحدهما لا يعني فشل
الآخر.

**الهدف:** ألا يتحول فشل Redis إلى 5xx أو إلى طلب معلّق. الكاش اختياري، والطابور
يحفظ الوظائف على القرص (`noeviction` + volume) قبل أن يلمسها العامل.

---

## 1) ما يفعله التطبيق تلقائياً

| المسار | عند تعطّل Redis | الدليل في الكود |
|---|---|---|
| قراءة كاش | `socket_timeout` = `REDIS_CACHE_TIMEOUT_SECONDS` (0.5s) ثم استثناء → تُسجّل miss ويُكمل الطلب من PostgreSQL | `app/platform/cache/redis.py`, `app/platform/cache/stats.py` |
| حد المعدل | عدّاد داخل العملية (fail-open تقريبي) بدل 500 | `app/core/ratelimit.py` |
| Idempotency | `_load` يعيد `None` بدل رفع الاستثناء | `app/api/middleware/idempotency.py` |
| الجاهزية | Redis/الطابور ⇒ `DEGRADED` مع HTTP 200؛ PostgreSQL وحده يعطي 503 | `app/core/health.py` |
| الطابور | `BLMOVE` محجوب 1s بحد socket 5s؛ الوظيفة تبقى في القائمة، و`recover()` يعيد تسليم ما انتهت مهلته (300s) | `app/platform/queue/redis.py`, `app/workers/main.py` |
| إعادة الاتصال | redis-py يعيد فتح الاتصال من الـpool عند عودة الخادم؛ لا حاجة لإعادة تشغيل العملية إذا بقي العنوان ثابتاً | — |

**ما لا يفعله التطبيق:** لا يدعم Sentinel/Cluster بروتوكولياً — يفتح عميلاً من
عنوان واحد. لذلك يجب أن يكون العنوان ثابتاً (نقطة نهاية مُدارة، DNS/VIP، أو
proxy) يتحول عند الـfailover. إن تغيّر العنوان نفسه، أعد تشغيل النشر بعد التبديل.

---

## 2) الاكتشاف (أول 60 ثانية)

```bash
# 1. الجاهزية: degraded مقبول، 503 ليس مقبولاً
curl -fsS https://api.example.com/ready | jq .
curl -fsS 'https://api.example.com/ready?strict=1' | jq .

# 2. هل الكاش يسقط فعلاً إلى قاعدة البيانات؟
curl -fsS https://api.example.com/metrics \
  | grep -E 'natiqo_cache_requests_total|natiqo_http_requests_total.*status="5'

# 3. اتصال Redis
redis-cli -u "$REDIS_URL" PING
redis-cli -u "$QUEUE_REDIS_URL" LLEN natiqo:jobs
redis-cli -u "$QUEUE_REDIS_URL" ZCARD natiqo:jobs:deadlines
redis-cli -u "$QUEUE_REDIS_URL" LLEN natiqo:jobs:dead
```

**الإنذارات المتوقعة:** ارتفاع `natiqo_cache_requests_total{result="miss"}`، وارتفاع
زمن قراءة الكتالوج/الملخص، و`/ready` بحالة `degraded`. لا تنطلق إنذارات 5xx إذا كان
الفشل محصوراً في Redis.

---

## 3) الإجراء

### أ) Redis مُدار (المفضّل)

1. من لوحة المزوّد: تحقق من الـfailover التلقائي واسم العقدة/الـendpoint.
2. إن تغيّر الـendpoint، حدّث `REDIS_URL`/`QUEUE_REDIS_URL` في Secret Manager ثم
   أعد تشغيل النشر (rolling). لا تعدّل `.env` على الخادم.
3. راقب عودة `natiqo_cache_requests_total{result="hit"}` و`/ready` إلى `ok`.

### ب) Redis على خادمين + Sentinel

1. رقِّي النسخة: `redis-cli -p 26379 SENTINEL failover natiqo-cache` (وبالمثل
   للطابور).
2. إن كنت تستخدم عنواناً ثابتاً (VIP/Proxy) فلا تغيير في التطبيق. وإلا فحدّث
   العنوان وأعد النشر.
3. تحقق من أن العامل يستهلك: `LLEN natiqo:jobs` يجب أن ينخفض، و`ZCARD ...:deadlines`
   لا يتراكم بلا نهاية.

### ج) تمارين الفوضى (staging فقط)

```bash
# الكاش: يوقف كل الأوامر 60 ثانية. المتوقع: صفر 5xx، زمن أعلى، /ready=degraded.
redis-cli -u "$REDIS_URL" CLIENT PAUSE 60000 ALL

# الطابور: نفس الاختبار. المتوقع: توقف الاستهلاك مؤقتاً ثم استئناف بلا فقدان،
# وربما تكرار وظيفة واحدة إذا انتهت مهلة 300s أثناء التوقف.
redis-cli -u "$QUEUE_REDIS_URL" CLIENT PAUSE 60000 ALL
```

سجّل في تمرينك: أقصى p95، عدد 5xx (يجب أن يكون 0)، مدة التعافي، وعدد الوظائف
المكررة. لا تُجرِ `FLUSHALL` أو `DEBUG SLEEP` في الإنتاج.

---

## 4) التحقق بعد العودة

- [ ] `/ready` = `ok` و`/ready?strict=1` = 200.
- [ ] `natiqo_cache_requests_total{result="hit"}` يعود للارتفاع.
- [ ] `natiqo_http_requests_total{status=~"5.."}` لم يزد أثناء النافذة.
- [ ] `natiqo:jobs:dead` لم يزد، و`ZCARD natiqo:jobs:deadlines` عاد للانخفاض.
- [ ] لا وظائف عالقة في `natiqo:jobs:processing` أقدم من 300s (`recover()` ينظفها).

## 5) التراجع

إذا تسبّب تبديل النسخة في سلوك أسوأ، أعد العنوان السابق وأعد النشر. لا تحذف
`natiqo:jobs` أو `natiqo:jobs:processing` للتراجع: حذفهما يُفقد وظائف حقيقية. بعد
استقرار الوضع يمكن تشغيل `recover()` يدوياً عبر دورة العامل العادية.

**RTO المستهدف:** ≤ 5 دقائق للكاش (فقدان أداء لا فقدان بيانات)، و≤ 15 دقيقة
للطابور مع عدم فقدان وظائف. **RPO:** صفر للطابور (الوظائف على القرص)، وصفر للكاش
(ليس مصدر حقيقة).
