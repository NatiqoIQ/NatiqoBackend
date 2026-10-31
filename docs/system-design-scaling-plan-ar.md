# خطة تصميم النظام والتوسّع — Natiqo Backend

> **الحالة:** مسودة للمراجعة (v1.0)
> **التاريخ:** 30 أكتوبر 2026
> **النطاق:** الـ Backend (FastAPI + PostgreSQL + Redis) + طريقة النشر والتشغيل + العقد مع تطبيق الموبايل
> **الهدف:** الوصول إلى **100,000 مستخدم نشط يومياً (DAU)** بأمان وبهامش أمان، مع مسار واضح إلى **300,000+**
> **المرجع التقني:** مراجعة الكود الحالي على فرع `main` (الالتزام `df42f8c`) — كل بند في هذه الخطة مربوط بملف أو سلوك موجود فعلاً، وليس نصيحة عامة.

---

## 0) الملخّص التنفيذي (TL;DR)

> **حالة التنفيذ:** شُحن 8 من 9 بنود P0 فعلياً (F1, F2, F3, F7, F32, F28, F4, F29)، ويبقى F6 جزئياً (Sentry/OTel). التفاصيل والأدلة في **§9.1**، ولا يُعتبر أي بند منجزاً إن لم يكن له اختبار في المستودع.

**الخلاصة في سطر واحد:** الكود الحالي **مصمَّم بشكل جيد جداً كنظام Modular Monolith**، لكنه **مُنشَّأ لبيئة تطوير/خادم واحد**، وليس لحركة مرور عشرات الآلاف. لا نحتاج إعادة كتابة؛ نحتاج **سلسلة إصلاحات محددة + طبقة Edge + ضبط قاعدة البيانات + طابور مهام موثوق + مراقبة حقيقية**.

### ما هو جيد فعلاً (لا نلمسه)
- فصل الطبقات (Domain / Application / Infrastructure / Presentation) وحدود واضحة بين الوحدات.
- `UnitOfWork` واحد لكل طلب، و`commit` صريح — يمنع الكتابة الجزئية.
- Keyset pagination، ETag/304، Idempotency-Key، وفصل Liveness عن Readiness.
- 935 اختباراً و23 migration و CI صارم (ruff + pyright + coverage ≥ 80%).
- Structured logging مع تنقية الأسرار، وتعامل جيد مع فشل مزوّد الـ AI (Fallback حتمي).

### أخطر 9 مشاكل يجب إصلاحها قبل أي حمل حقيقي
| # | المشكلة | لماذا قاتلة عند 100k DAU |
|---|---|---|
| 1 | **Rate limit لكل IP** (`app/api/deps.py`) | شبكات الموبايل في العراق تستخدم **CGNAT**: آلاف المستخدمين يظهرون بعنوان IP واحد → حجب جماعي مشروعين |
| 2 | **طلب AI يحتفظ باتصال قاعدة بيانات أثناء انتظار المزوّد حتى 20 ثانية** (`tutor_service.py` + `uow_dep`) | 100 طلب AI متزامن = 100 اتصال محجوز بلا فائدة → استنفاد الـ pool وشلل كل الـ API |
| 3 | **`alembic upgrade head` يعمل عند كل إقلاع للحاوية** (`Dockerfile`) | مع 3+ نسخ أثناء الـ rolling deploy تتسابق على نفس الـ migration → فشل إقلاع أو أقفال |
| 4 | **الطابور Redis List بضمان “مرة واحدة على الأكثر”** (`BLPOP`) + مستهلك واحد + retry نائم داخل الطابور | وظيفة تُسحب ثم يموت الـ worker = **فقدان دائم**؛ ووظيفة سيئة تعطّل كل الوظائف الأخرى 30 ثانية × 5 محاولات |
| 5 | **خادم واحد + قاعدة بيانات واحدة + لا نسخ احتياطي مُختبَر** (`docker-compose.yml`, `deploy.yml`) | أي عطل قرص/جهاز = توقف كامل واحتمال فقدان بيانات |
| 6 | **لا Observability حقيقية**: `OTEL_ENABLED` و`SENTRY_DSN` موجودان في الإعدادات ولا يقرأهما أي كود | لا يمكن تشغيل نظام بهذا الحجم بالتخمين؛ لن نعرف أي endpoint يبطئ أو أي استعلام يقتل القاعدة |
| 7 | **لا Cache على الخادم للكتالوج**: الـ `ETag` يخدّم العميل فقط، و`tree()` يبني الشجرة بـ (1 + عدد المستويات + عدد الوحدات) استعلاماً | شجرة من 6 مستويات و30 وحدة = ~37 استعلاماً لكل طلب؛ ولو كانت كلها لشجرة الكتالوج عند 500 طلب/ثانية = **18,500 استعلام/ثانية** على PostgreSQL |
| 8 | **الوسائط تُقدَّم من نفس الحاوية** (`StaticFiles` + volume محلي) | كل تحميل صوت/صورة يستهلك CPU وعرض نطاق الـ API، ولا يعمل مع أكثر من نسخة |
| 9 | **Redis تبعية صلبة**: `rate_limit` و`Idempotency` يفشلان 500 عند تعطّله، و`/ready` يعتبره `UNHEALTHY` | انقطاع Redis لدقيقة يسحب كل النسخ من الـ LB ويوقف التطبيق، رغم أن الكاش ليس مصدر حقيقة |

### الاستراتيجية (4 محاور)
1. **قِس أولاً**: Metrics + Traces + Sentry + Load test Baseline قبل تحسين أي شيء.
2. **وسّع بلا حالة**: API pods متعددة، Edge/CDN، PgBouncer، Redis HA.
3. **خفّف القاعدة**: Cache للكتالوج، عدّادات جاهزة بدل التجميع اللحظي، فهارس مصممة للاستعلامات، تقليم/أرشفة للبيانات.
4. **أخرج العمل غير الحرج من الطلب**: طابور موثوق + Outbox للإشعارات والتحليلات، مع إبقاء XP/streak متزامناً لأن الصحة أهم من السرعة.

### الطريق الزمني
| المرحلة | المدة | الهدف | معيار الخروج |
|---|---|---|---|
| **Phase 0 — نرى ونحمي** | أسبوعان | إصلاحات P0 + Baseline | حمل 200 RPS، لا أخطاء 5xx، لوحة + 10 تنبيهات |
| **Phase 1 — 20k DAU** | 4 أسابيع | Edge + PgBouncer + Cache + Streams | 500 RPS لمدة 30 دقيقة، p95 < 250ms |
| **Phase 2 — 100k DAU** | 6 أسابيع | Read replica + Retention + Counters + DR | 2,000 RPS، Soak 24 ساعة، استعادة < ساعة |
| **Phase 3 — 300k+** | 8–12 أسبوعاً | Cells/Sharding عند الحاجة + CDC + تحسين التكلفة | 5,000 RPS، تكلفة/1k طلب محددة |

### ما لن نفعله الآن (وهذا قرار هندسي، لا تقصير)
لن ننتقل إلى **Microservices**، ولا **Kafka**، ولا **Kubernetes**، ولا **Sharding**، ولا **Multi-Region Active-Active** في هذه المرحلة. كل واحدة منها لها **شرط تفعيل قابل للقياس** مذكور في §13. الانتقال المبكر يضاعف التعقيد ويستهلك الفريق دون فائدة.

---

## 1) المنهجية والافتراضات

### 1.1 كيف بُنيت هذه الخطة
- قراءة كاملة لطبقات `app/` والـ adapters في `app/platform/` والـ worker وDocker/CI.
- تتبّع المسارات الحرجة: تسجيل الدخول، بدء الدرس، الإجابة، إكمال الدرس، شاشة التقدم، الكتالوج، AI.
- حساب السعة من عدد الطلبات المتوقعة (وليس من الحدس)، مع توضيح كل افتراض.
- **لا توجد أرقام إنتاج حالية** (لا Metrics، لا Load tests، لا بيانات). لذلك كل أرقام §4 هي **افتراضات يجب استبدالها بـ Baseline حقيقي في Phase 0**.

### 1.2 افتراضات العمل
| المتغيّر | القيمة المفترضة | ملاحظة |
|---|---|---|
| الحسابات المسجّلة | 300,000 | |
| MAU | 150,000 (50%) | |
| **DAU** | **100,000** | الهدف التصميمي |
| الذروة المتزامنة | 12,000 (12% من DAU) | 20:00–23:00 بتوقيت بغداد |
| جلسات/مستخدم/يوم | 2–3 | |
| طلبات/مستخدم/يوم | 80–120 | بعد تفعيل الكاش على العميل والـ CDN |
| إجمالي الطلبات/يوم | 8–12 مليون | |
| معامل الذروة | 5× المتوسط | العادات اليومية للتعلم مسائية |
| RPS الذروة | 500–700 | |
| **هدف الاختبار** | **2,000 RPS** | هامش ~3× فوق الذروة المتوقعة |
| نسبة القراءة/الكتابة | 75% / 25% | بعد الكاش |
| موقع المستخدمين | العراق (+ شتات) | زمن الوصول للخوادم الأوروبية 60–90ms |

### 1.3 افتراضات محتوى المنهج
- 6 مستويات × 5 وحدات × 8 دروس = 240 درساً.
- 10–14 سؤالاً لكل درس.
- الشجرة كاملة < 100KB بعد الضغط (مناسبة لاستجابة واحدة وCDN).

> **قاعدة الحوكمة:** إذا تجاوز أي رقم في §4 نظيره المقاس بنسبة 30%، تُحدَّث الخطة قبل الانتقال للمرحلة التالية. الأرقام ليست وعوداً، بل فروض اختبار.

---

## 2) الوضع الحالي — نقاط القوة (نحافظ عليها)

| القوة | الدليل | لماذا تهمّ للتوسع |
|---|---|---|
| Modular Monolith بحدود واضحة | `app/modules/*` + `app/core/uow.py` | يمكن استخراج خدمة لاحقاً دون إعادة كتابة |
| كل شيء عبر Ports/Adapters | `app/platform/*/port.py` | تبديل Redis/SQS/S3 لا يمسّ الـ Domain |
| UoW + commit صريح | `app/core/uow_sqlalchemy.py`, `app/api/deps.py` | يمنع الكتابة الجزئية عند الفشل |
| Keyset pagination | `app/core/pagination.py` | لا `OFFSET` ينهار مع نمو البيانات |
| ETag + 304 + Cache-Control | `app/core/http_cache.py` | أساس ممتاز لـ CDN |
| Idempotency-Key | `app/core/idempotency.py` | ضروري لشبكات الموبايل غير المستقرة |
| Liveness/Readiness منفصلان | `app/core/health.py` | يمنع crash loops عند تعطّل القاعدة |
| Structured logs + Redaction | `app/core/logging.py` | جاهز للـ aggregation |
| AI اختياري مع Fallback | `app/platform/ai/service.py` | تعطّل المزوّد لا يوقف التعلم |
| 935 اختبار + CI صارم | `tests/`, `.github/workflows/ci.yml` | يسمح بالتحسين بثقة |

---

## 3) الفجوات والمخاطر مرتّبة

### 3.1 جدول ملخّص
| ID | الخطورة | المشكلة | المرحلة |
|---|---|---|---|
| F1 | **P0** | Rate limit لكل IP ونقص ثقة بالـ Proxy (CGNAT) | 0 |
| F2 | **P0** | اتصال قاعدة بيانات محجوز أثناء نداء AI (حتى 20s) | 0 |
| F3 | **P0** | Migration على كل إقلاع للحاوية | 0 |
| F4 | **P0** | طابور غير موثوق (at-most-once) + مستهلك واحد + لا DLQ | 0 (أمان) / 1 (استبدال) |
| F5 | **P0** | خادم واحد + لا HA + لا نسخ احتياطي مُختبَر | 0 |
| F6 | **P0** | لا Metrics/Traces/Sentry رغم وجود الإعدادات | 0 |
| F7 | **P0** | لا Timeouts على DB/الطلبات + pool_timeout = 30s | 0 |
| F8 | **P1** | كتالوج بلا Cache على الخادم + N+1 في `tree()` | 1 |
| F9 | **P1** | الوسائط تُقدَّم من الحاوية | 1 |
| F10 | **P1** | `progress.summary` يجمّع تاريخ المستخدم كاملاً في كل طلب | 2 |
| F11 | **P1** | فحص فتح الدرس يحمّل كل الدروس وتقدم المستخدم | 2 |
| F12 | **P1** | Refresh Token: كتابة DB كل 15 دقيقة + Purge دفعة واحدة | 1 |
| F13 | **P1** | AI: لا سقف عالمي، لا Bulkhead، Metering بجلسة DB لكل نداء | 2 |
| F14 | **P1** | Redis واحد للكاش والطابور (خطر Eviction للوظائف) | 1 |
| F15 | **P1** | لا Partitioning/Retention للجداول الضخمة | 2 |
| F16 | **P1** | Pool size غير مرتبط بعدد النسخ + لا PgBouncer | 1 |
| F17 | **P1** | Access log لكل طلب + `/ready` يضرب DB/Redis في كل Probe | 1 |
| F18 | **P1** | Idempotency يخزّن استجابات كاملة 24h في نفس Redis | 2 |
| F19 | **P1** | Admin stats: `COUNT(*)` على جداول كاملة | 2 |
| F20 | **P1** | لا Load tests/Synthetic monitoring في CI | 0 |
| F21 | **P2** | UUIDv4 كمفتاح أساسي في جداول Append-heavy | 3 |
| F22 | **P2** | لا Canary/Blue-Green؛ البناء على خادم الإنتاج | 2 |
| F23 | **P2** | الأسرار في `.env` على الخادم + لا WAF/DDoS | 2 |
| F24 | **P2** | لا Feature flags ولا حوكمة تكلفة AI | 3 |
| F25 | **P2** | لا تدوير لمفاتيح JWT (`kid`) | 3 |
| F26 | **P2** | لا استخدام لـ Read Replica | 2 |
| F27 | **P2** | لا CDC/Warehouse للتحليلات | 3 |
| F28 | **P1** | الوسائط تُرسَل بـ `Cache-Control: no-store` فلا يخزّنها العميل ولا CDN | 1 |
| F29 | **P1** | Idempotency لا يمنع تنفيذ طلبين متزامنين بنفس المفتاح | 2 |
| F30 | **P2** | سباق إجابتين متزامنتين لنفس السؤال → `IntegrityError` (500) | 2 |
| F31 | **P2** | حذف سؤال يحذف إجابات المتعلمين (`ON DELETE CASCADE`) | 3 |
| F32 | **P0** | تعطّل Redis يتحول إلى 500 لكل الطلبات، و`/ready` يسحب كل النسخ | 0 |

---

### 3.2 تفصيل مشاكل P0

#### F1 — Rate limiting لكل IP: سقوط جماعي على شبكات CGNAT
**الدليل:** `app/api/deps.py` → `client_ip()` + `rate_limit()`؛ المفتاح هو `scope + IP + window`. و`trust_forwarded_for` افتراضياً `False`، و`uvicorn` يعمل بلا `--proxy-headers`.
**الأثر:** في العراق، مشغّلو الموبايل يستخدمون CGNAT؛ آلاف المستخدمين يشاركون IP واحداً. الحد الافتراضي 120/دقيقة **لكل IP** سيحوّل الذروة المسائية إلى 429 جماعي. وبوجود LB بدون ثقة بالـ proxy، سيرى التطبيق عنوان الـ LB فقط → **كل المستخدمين في دلو واحد**.
**الحل:**
1. الهوية الأساسية للحد = **المستخدم** (بعد التحقق)، وليس IP. وقبل التحقق استخدم **بصمة التوكن** (SHA-256) كما يفعل `idempotency._scope_user_id`، مع fallback إلى IP للمجهول.
2. ارفع حدود IP للمجهول إلى قيمة تتحمل CGNAT (مثلاً 3,000/دقيقة لكل IP) واجعل الحماية الحقيقية من DDoS على الـ Edge.
3. فعّل `--proxy-headers --forwarded-allow-ips=<CIDR الـ LB>` و`TRUST_FORWARDED_FOR=true` **فقط** خلف proxy تملكه.
4. استبدل النافذة الثابتة بـ **Token Bucket** عبر Lua ذرّي (يمنع انفجار الحدود المزدوج عند حدود النافذة).
5. أضف مقاييس `ratelimit_rejected_total{scope, principal_type}` لمراقبة الإنذارات الكاذبة.
**معيار القبول:** اختبار حمل بـ 1,000 VU من IP واحد مع 1,000 توكن مختلف → نسبة 429 < 1%، ولا يتأثر مستخدم شرعي.

#### F2 — طلب AI يحتجز اتصال قاعدة بيانات أثناء نداء خارجي
**الدليل:** `app/api/deps.py::uow_dep` يفتح الجلسة لكل طلب. `AiTutorService.ask()` يقرأ المحادثة، ثم `await self._ai.tutor_reply(...)` (Timeout 20s)، ثم يكتب ويُنهي. `AuthService.authenticate` يفتح الاتصال في بداية كل طلب موثّق أيضاً.
**الأثر:** الاتصال يبقى محجوزاً طوال مدة نداء المزوّد. `pool_size=10` + `max_overflow=20` لكل عملية؛ 30 طلب AI متزامناً كافية لاستنفاد pool نسخة واحدة. ومع `pool_timeout=30s`، تتحول المشكلة إلى **طابور انتظار 30 ثانية** لكل الـ API قبل الفشل.
**الحل:**
1. اقسم العملية إلى 3 معاملات قصيرة: (قراءة السياق) → (نداء المزوّد **بدون** جلسة) → (كتابة النتيجة).
2. اضبط `AI_TIMEOUT_SECONDS` إلى 8–10s، مع Fallback فوري.
3. أضف **Bulkhead**: Semaphore لكل عملية (مثلاً 20 نداء AI متزامناً) و503 سريع عند التجاوز بدل الانتظار.
4. أضف Circuit Breaker على المزوّد (فشل 5 متتالٍ → فتح 60 ثانية → Fallback).
5. على المدى المتوسط: حوّل النداءات الطويلة إلى Job مع Polling/SSE.
**معيار القبول:** 500 طلب AI متزامن مع مزوّد “بطيء” (اختبر بـ 20s delay) → Pool in-use < 40%، وبقية الـ API p95 لا يتغير.

#### F3 — Migrations على كل إقلاع
**الدليل:** `Dockerfile` → `CMD ["sh","-c","alembic upgrade head && exec uvicorn ..."]`.
**الأثر:** أول Deploy بنسختين سيتسابقان على نفس القفل؛ ومع migration طويلة (ALTER على جدول كبير) تتضاعف مدة التوقف. كما أن الـ worker يبدأ بعد صحة الـ app، لكن أي نسخة جديدة تُقلع ستكرر المحاولة.
**الحل:**
1. افصل الـ migration في **Job/خدمة مستقلة** تعمل مرة واحدة قبل النشر (`docker compose run --rm migrate` أو Kubernetes Job).
2. ضع **Advisory Lock** داخل `alembic/env.py` (`pg_advisory_lock`) كحماية أخيرة.
3. تبنّى **Expand/Contract**: أضف الأعمدة/الجداول في إصدار، واستخدمها في الإصدار التالي، واحذف القديم في الثالث.
4. في Docker Compose: خدمة `migrate` بـ `restart: "no"` و`depends_on` صحي، ثم `app` تعتمد عليها بـ `service_completed_successfully`.
**معيار القبول:** Rolling deploy بـ 3 نسخ أثناء migration → صفر أخطاء إقلاع، وصفر أقفال مكررة.

#### F4 — طابور غير موثوق
**الدليل:** `app/platform/queue/redis.py` يستخدم `RPUSH`/`BLPOP`؛ لا ACK ولا visibility timeout. `app/workers/main.py` يعالج وظيفة واحدة ويستخدم `asyncio.sleep` للـ retry داخل نفس الحلقة. `dedupe` يُضبط عند الإدخال ولا يُحرَّر عند الفشل. لا DLQ دائم — اللوج هو السجل.
**الأثر:**
- سحب وظيفة ثم موت العملية = **فقدان نهائي**.
- وظيفة سامة تعطّل المستهلك الوحيد حتى 5 محاولات × backoff.
- إعادة إدخال نفس الوظيفة بعد فشل دائم ممنوعة لمدة ساعة بسبب `dedupe`.
- Redis مشترك مع الكاش؛ سياسة `allkeys-lru` قد **تحذف وظائف**.
**الحل الفوري (Phase 0):** نقل الطابور إلى **Redis instance منفصل** بـ `maxmemory-policy noeviction`، وإضافة `BRPOPLPUSH` إلى قائمة `processing` مع `visibility timeout` واسترجاع دوري.
**الحل النهائي (Phase 1):** **Redis Streams + Consumer Groups** (`XADD`/`XREADGROUP`/`XACK`/`XAUTOCLAIM`) أو SQS:
- Acknowledgment بعد النجاح فقط.
- Retry مع backoff و`attempts` في الرسالة.
- **DLQ Stream** بعد 5 محاولات.
- Concurrency داخل الـ worker (`Semaphore`، 20–50 وظيفة).
- Idempotency في الـ handler (مفتاح العملية).
- مراقبة `lag` و`pending` و`DLQ`.
**معيار القبول:** قتل الـ worker في منتصف وظيفة → تُعاد الوظيفة خلال 60 ثانية؛ 1% وظائف فاشلة → تذهب إلى DLQ ولا تعطّل غيرها؛ لا وظائف مفقودة في اختبار 10,000 وظيفة.

#### F5 — نقطة فشل واحدة ولا نسخ احتياطي مُختبَر
**الدليل:** `docker-compose.yml` (app + db + redis على جهاز واحد)، `deploy.yml` ينشر على خادم LAN واحد (`192.168.69.67`) بـ `git reset --hard` و`docker compose up --build`.
**الأثر:** عطل القرص/الجهاز/الـ Docker daemon = توقف كامل. لا WAL archiving أو PITR. `pgdata` volume ليس نسخة احتياطية.
**الحل:**
1. PostgreSQL مُدار بـ HA (أو Patroni على 3 عُقد) + **PITR** (WAL) + نسخة يومية لمدة 30 يوماً + نسخة عبر المنطقة.
2. اختبار استعادة **شهري** إلى بيئة معزولة، مع قياس RTO/RPO.
3. Redis بـ Sentinel/Cluster، وعدم الاعتماد عليه كمصدر حقيقة.
4. نشر على عقدتين+ خلف LB، وصور Docker مبنية في CI ومسحوبة بالـ digest (لا بناء على الإنتاج).
5. Object Storage للوسائط (لا volume محلي).
**معيار القبول:** استعادة كاملة إلى نقطة قبل 15 دقيقة في < 60 دقيقة، موثقة ومُجرَّبة.

#### F6 — لا Observability
**الدليل وقت المسح:** `app/core/config.py` يحتوي `OTEL_ENABLED`, `SENTRY_DSN`، لكن `grep` لم يجد أي مستهلك لهما. لم يكن هناك `/metrics`؛ كان مذكوراً في `_QUIET_PATHS` فقط. **تحديث:** `/metrics` شُحن في §9.1، ويبقى Sentry/OTel بلا مستهلك.
**الأثر:** بلا مقاييس ستُشخّص كل حادثة بعد فوات الأوان، ولن تعرف أثر أي تحسين.
**الحل:** راجع §7.10 كاملاً. الحد الأدنى في Phase 0:
- `/metrics` (Prometheus) لتأخير HTTP، معدل الأخطاء، pool DB، Redis، الطابور.
- OTel traces (FastAPI + SQLAlchemy + redis + httpx) بعينة 10% (100% للأخطاء).
- Sentry للأخطاء غير المعالجة.
- Dashboards: Golden Signals + قاعدة البيانات + الطابور + AI.
**معيار القبول:** أي طلب بطيء يمكن تتبّعه من الـ LB إلى الاستعلام.

#### F7 — لا Timeouts ولا Load Shedding
**الدليل:** `app/core/database.py` لا يضبط `statement_timeout`/`lock_timeout`؛ `pool_timeout=30`. `Dockerfile` يشغّل uvicorn بلا `--limit-concurrency` أو `--timeout-keep-alive`؛ لا timeout على الطلبات.
**الأثر:** استعلام واحد بطيء (أو قفل) يحتجز اتصالاً؛ تراكم الطلبات يملأ الـ pool؛ لا يوجد رفض سريع → تفاقم الانهيار بدل degradable failure.
**الحل:**
```python
connect_args={
    "server_settings": {
        "application_name": "natiqo-api",
        "statement_timeout": "4000",
        "lock_timeout": "1500",
        "idle_in_transaction_session_timeout": "8000",
    },
}
# pool_timeout = 2–3 ثوانٍ، لا 30
```
وأضف إلى uvicorn: `--limit-concurrency 400 --backlog 2048 --timeout-keep-alive 15 --timeout-graceful-shutdown 25`.
**معيار القبول:** عند 3× الحمل، يفشل 5% من الطلبات بـ 503 سريع (< 1s) بدل تعليق النظام.

#### F32 — Redis أصبح تبعية صلبة في مسار الطلب وفي الـ Readiness
**الدليل:**
- `app/api/deps.py::rate_limit` ينادي `cache.increment` بلا `try/except`؛ أي خطأ اتصال يصبح 500.
- `app/core/idempotency.py::_load` ينادي `cache.get` بلا حماية؛ أي طلب كتابة يحمل `Idempotency-Key` يفشل.
- `app/core/health.py::build_health_report` يعتبر أي فحص غير الـ queue فاشلاً → `UNHEALTHY` (503)، فيسحب الـ LB **كل** نسخ الـ API أثناء failover لـ Redis.
**الأثر:** انقطاع Redis لدقائق (أو إعادة تشغيل/ترقية) يوقف التطبيق بالكامل، رغم أن الكاش والطابور **ليسا** مصدر الحقيقة. هذا يخالف مبدأ “فشل متدرّج” في §6.2.
**الحل:**
1. اجعل كل عمليات الكاش **Fail-open** للمسارات الحرجة: إذا فشل Redis، اسمح بالطلب وسجّل `cache_unavailable` (مع Circuit Breaker قصير حتى لا تنتظر كل طلب).
2. Rate limit: fallback إلى عدّاد داخل العملية (In-memory) عند تعطّل Redis، مع حد أعلى مؤقت.
3. Idempotency: إذا تعذّر القراءة/الكتابة، نفّذ الطلب عادةً (مثل سلوك ما قبل الميزة) ولا تُفشله.
4. Readiness: عامل `cache` و`queue` كـ **DEGRADED** لا `UNHEALTHY`؛ الـ 503 يبقى حصراً لتعذّر PostgreSQL. أضف `?strict=1` لفحص تشغيلي يدوي.
5. اكتب اختبار فشل: أوقف Redis أثناء الحمل → صفر 5xx، والـ `/ready` يبقى 200 مع `status=degraded`.
**معيار القبول:** إيقاف Redis 60 ثانية أثناء 500 RPS → 0 أخطاء 5xx، واستعادة الكاش تلقائياً بعد عودته.

---

### 3.3 تفصيل مختصر لمشاكل P1/P2

| ID | التفصيل | الأثر | الحل |
|---|---|---|---|
| **F8** | `CatalogueService.tree()` يستدعي `list_levels` ثم لكل level `list_units` ثم لكل unit `list_lessons` — كلها استعلامات متسلسلة. لا Redis Cache؛ `@cacheable` يضع Headers فقط. | ~37 استعلاماً/طلب؛ زمن إضافي متسلسل. أي موجة إقلاع صباحية تضرب القاعدة. | Cache-Aside في Redis بمفتاح نسخة (`content:tree:v{n}`) + Single-Flight + TTL 5–15 دقيقة + Jitter؛ إبطال عند النشر؛ CDN أمام المسار. |
| **F9** | `app/main.py` يركّب `StaticFiles` على `/media` من volume محلي. | كل نسخة API تخدم الوسائط؛ لا Cache؛ عرض نطاق مكلف. | S3/R2 + CDN، مسارات `immutable`, WebP/AVIF، Opus 32–48kbps. |
| **F10** | `ProgressService.summary()` ينفّذ 5 استعلامات، منها `tally_for_user` (كل إجابات المستخدم مدى الحياة) و`list_completion_times` (كل الجلسات). | يزداد خطياً مع عمر الحساب؛ بعد سنتين قد يقرأ آلاف الصفوف لكل فتح شاشة. | عدّادات مجمّعة في `user_stats` تُحدَّث في معاملة الإكمال + Cache 60s للملخص. |
| **F11** | `_assert_lesson_unlocked` و`_assert_unit_open` يحمّلان `list_published_ordered()` + كل صفوف تقدم المستخدم عند **بدء** كل جلسة. | استهلاك ثابت لكنه كبير لكل درس؛ يتضاعف مع المحتوى. | Cache لخريطة الفتح لكل مستخدم (60s) + إبطال عند الإكمال؛ أو مادة `unlock_state` محسوبة. |
| **F12** | Access token 15 دقيقة → ~12 عملية Refresh/مستخدم/يوم نشط؛ كل Refresh معاملة كتابة (قراءة + إدراج + تحديث). Purge يومي دفعة واحدة. | ~1.2M صف/يوم في `refresh_tokens`؛ جدول منتفخ؛ Purge يسبب قفزات I/O. | رفع TTL إلى 30 دقيقة (نصف الكتابات)، Purge على دفعات 5k بـ `ctid`, أرشفة شهرية. |
| **F13** | `record_ai_usage` ينشئ Task + جلسة DB مستقلة لكل نداء؛ لا سقف عالمي؛ `AI_DAILY_USER_BUDGET_USD` فقط. | Churn اتصالات؛ تكلفة غير محدودة عند إساءة استخدام أو خطأ. | تجميع الاستخدام في دفعات، سقف يومي عالمي + لكل feature، Bulkhead، Circuit Breaker. |
| **F14** | نفس `REDIS_URL` للكاش والطابور (`deps._build_cache` و`_build_queue`). | `allkeys-lru` قد يحذف وظائف؛ `noeviction` يوقف الكاش. | Redis منفصلان (أو Cluster منفصل) بسياسات ذاكرة مختلفة. |
| **F15** | `quiz_answers`, `review_logs`, `ai_usage_records`, `audit_logs`, `refresh_tokens`, `user_daily_challenge_contributions` بلا Partitioning/Retention. | مئات GB سنوياً؛ فهارس تتضخم؛ VACUUM أبطأ. | Partitioning للجداول Append-only بلا قيود Unique عابرة، وRetention/Audit للأخرى (شُحن حذف التوكنات بدفعات). |
| **F16** | `DATABASE_POOL_SIZE=10` ثابت؛ لا PgBouncer. | مع 20 نسخة → 200–600 اتصال؛ PostgreSQL ينهار فوق ~300–500. | معادلة pool في §7.3 + PgBouncer transaction mode. |
| **F17** | Access log لكل طلب في `RequestContextMiddleware`؛ `/ready` يضرب DB + Redis في كل Probe. | ملايين الأسطر يومياً؛ Probe كل 5s × N نسخ = حمل زائد. | Sampling 1:10 للطلبات الناجحة، 100% للأخطاء؛ Cache نتيجة `/ready` لـ 3–5s. |
| **F18** | Idempotency يخزّن جسم الاستجابة كاملاً (مضغوطاً) 24h في Redis. | استهلاك ذاكرة كبير؛ منافسة مع الكاش. | حد أقصى 64KB، TTL متدرج، Redis منفصل، تجاهل الاستجابات الكبيرة. |
| **F19** | `stats_repository` يستخدم `COUNT(*)` و`GROUP BY date` على جداول كاملة. | شاشة Admin واحدة قد توقف القاعدة. | جدول Rollup يومي + Read Replica + Cache 5 دقائق. |
| **F20** | لا k6/Locust ولا Synthetic monitoring. | لا نعرف أين “الركبة” قبل الإنتاج. | §10: سيناريوهات k6 في CI الليلي + تحقق خارجي. |
| **F21** | `UUIDPrimaryKeyMixin` = UUIDv4 عشوائي. | إدراج عشوائي في B-Tree → انقسام صفحات وBloat في الجداول الضخمة. | UUIDv7 (أو `bigint identity`) للجداول الجديدة Append-heavy؛ ترحيل الجداول الكبيرة فقط بعد قياس. |
| **F22** | لا Canary؛ النشر `reset --hard` + build على الإنتاج. | Rollback صعب؛ توقف أثناء البناء. | صور Immutable + Deploy بالـ digest + Canary 5% ثم 25% ثم 100%. |
| **F23** | أسرار في `.env`؛ لا WAF. | تسريب أو DDoS يوقف الخدمة. | Secret Manager + WAF + قواعد DDoS + Least Privilege. |
| **F24** | لا Feature flags. | كل تغيير يحتاج نشراً كاملاً. | مزوّد Flags (أو جدول + Cache) للإطلاق التدريجي. |
| **F25** | HS256 بمفتاح واحد؛ لا `kid`. | تدوير المفاتيح يتطلب توقفاً. | RS256/EdDSA + `kid` + نشر مفاتيح عامة. |
| **F26** | لا Read Replica. | كل القراءات على الـ Primary. | Replica للقراءات المتسامحة مع التأخير فقط (§7.3). |
| **F27** | لا CDC/Warehouse. | تقارير ثقيلة على قاعدة المعاملات. | CDC (Debezium) → Warehouse + جدول Rollups. |
| **F28** | `SecurityHeadersMiddleware` يضع `Cache-Control: no-store` على كل استجابة لا تحمل `@cacheable`، و`StaticFiles` لا يضع سياسة تخزين. | كل تشغيل لملف صوت/صورة يعيد التحميل من الـ API؛ لا CDN ولا كاش عميل؛ عرض نطاق هائل لتطبيق لغة. | اسمح لمسارات `/media` بإعلان `public, max-age=31536000, immutable` (اسم ملف بإصدار/hash)، أو قدّمها من CDN مباشرة. أضف اختباراً يتأكد من الـ Header. واضبط `Cross-Origin-Resource-Policy: cross-origin` للوسائط إن كان نطاق الـ CDN مختلفاً. |
| **F29** | `IdempotencyMiddleware` يقرأ المفتاح ثم ينفّذ؛ لا قفل «قيد التنفيذ». | إعادة إرسال سريعة (أو ضغط مزدوج) تنفّذ الطلب مرتين قبل تخزين النتيجة. | `SET NX` كقفل تنفيذ + انتظار قصير + إعادة قراءة النتيجة، أو `409 CONFLICT_IN_PROGRESS`. |
| **F30** | `submit_answer` يقرأ الإجابة السابقة ثم يُدرج/يحدّث؛ لا قفل صف. | إجابتان متزامنتان لنفس السؤال تصطدمان بـ `uq_quiz_answers_session_question` → 500. | `INSERT ... ON CONFLICT (session_id, question_id) DO NOTHING` ثم قراءة إجابة الفائز وإعادة حكمها. |
| **F31** | `quiz_answers.question_id` و`review_cards.question_id` بـ `ON DELETE CASCADE`. | حذف سؤال من المنهج يمحو تاريخ إجابات المتعلمين ومراجعاتهم. | ارفض الحذف إن وُجد أي `quiz_answers` للسؤال (409)؛ الإخفاء يكون بإبقاء الدرس غير منشور. |

---

## 4) نموذج السعة (Capacity Model)

### 4.1 الحمل
| البند | الحساب | النتيجة |
|---|---|---|
| الطلبات اليومية | 100,000 × 100 | 10,000,000 |
| متوسط RPS | 10M / 86,400 | **116** |
| ذروة RPS | 116 × 6 | **~700** |
| هدف التصميم | 700 × ~3 | **2,000 RPS** |
| قراءات الذروة (75%) | | 1,500 RPS |
| كتابات الذروة (25%) | | 500 RPS |
| استعلامات DB/ثانية (تقديري) | 700 × 6 | **4,200** |
| بدون Cache للكتالوج | 30% من الطلبات × 37 استعلاماً | **+7,800 استعلام/ثانية** ← غير مقبول |

**الاستنتاج:** الكاش على الكتالوج ليس “تحسيناً”، بل **شرط بقاء**. بدونه تتضاعف استعلامات القاعدة حتى مع 20k DAU.

### 4.2 اتصالات قاعدة البيانات
| النوع | RPS ذروة | زمن المعاملة p95 | اتصالات مطلوبة |
|---|---|---|---|
| قراءة | 1,500 | 20ms | 30 |
| كتابة | 500 | 60ms | 30 |
| **الإجمالي النظري** | | | **60** |
| مع هامش 3× | | | **180** |
| PgBouncer pool | | | **80–120** |
| حد PostgreSQL الآمن | | | **≤ 300** |

> **قاعدة:** `Pool per pod = ceil(connections_needed / pods) + 2`. لا تضبطه مرة واحدة وتنساه. عند 10 نسخ: pool=12 لكل نسخة + overflow 3 = 150 كحد أقصى.

### 4.3 النمو التخزيني (تقديري)
| الجدول | صفوف/يوم | حجم/سنة (مع الفهارس) | السياسة |
|---|---|---|---|
| `quiz_answers` | 2,000,000 | 250–350 GB | Retention 24 شهراً + أرشفة |
| `quiz_sessions` | 200,000 | 25–40 GB | Partition شهري + Retention 24 شهراً |
| `review_logs` | 500k–1M | 60–100 GB | Partition شهري + Retention 12 شهراً |
| `refresh_tokens` | 800k–1.2M | 80–120 GB | Purge على دفعات + Retention 90 يوماً بعد الانتهاء |
| `ai_usage_records` | 20k–100k | 5–15 GB | Partition شهري + Retention 24 شهراً |
| `ai_messages` | 50k–200k | 10–30 GB | Retention 12 شهراً أو أرشفة المحادثات |
| `audit_logs` | 5k–20k | 2–5 GB | Partition شهري + Retention 7 سنوات (امتثال) |
| `user_daily_challenge_contributions` | 200k | 10–20 GB | Retention 24 شهراً |
| **الإجمالي** | | **~0.5–0.8 TB/سنة** | |

> هذه الأرقام تعني أن **Retention ليس ترفاً**: بدونه سيتجاوز الجدول الواحد حدود الفهرسة والصيانة خلال سنة.

### 4.4 Redis والشبكة والسجلات
| البند | الذروة | ملاحظة |
|---|---|---|
| عمليات Redis | 3,000–6,000 ops/s | Cache + Rate limit + Queue |
| ذاكرة Cache | 1–3 GB | مع TTL وتقليم |
| ذاكرة Queue/Dedupe | 100–500 MB | **منفصلة** |
| Egress JSON | 15–30 GB/يوم | مع Gzip/Brotli |
| Egress وسائط | 150–400 GB/يوم | **يجب أن يكون CDN** |
| سجلات | 4–8 GB/يوم | Sampling ضروري |

### 4.5 ما يجب قياسه لاستبدال الافتراضات
- RPS الفعلي وتوزيعه على المسارات (من Access logs).
- p50/p95/p99 لكل مسار.
- عدد الاستعلامات لكل طلب (SQLAlchemy events أو APM).
- Cache hit ratio.
- Pool in-use/waiters.
- متوسط مدة المعاملة.
- عدد المستخدمين النشطين في الذروة.
- حجم البيانات والنمو اليومي.

---

## 5) أهداف مستوى الخدمة (SLOs)

| SLI | SLO | نافذة | ميزانية الخطأ | تنبيه |
|---|---|---|---|---|
| توفر الـ API (2xx+3xx+4xx) | 99.9% | 30 يوماً | 43 دقيقة | Burn rate 2% في ساعة |
| توفر قراءة الكتالوج | 99.95% | 30 يوماً | 21 دقيقة | Burn rate 5% في 10 دقائق |
| نسبة أخطاء 5xx | < 0.5% | 30 يوماً | — | > 2% لمدة 5 دقائق |
| p95 لقراءة الكتالوج | < 120ms (Edge) / < 250ms (Origin) | 7 أيام | — | > 300ms لـ 10 دقائق |
| p95 لكتابة إجابة | < 300ms | 7 أيام | — | > 500ms لـ 10 دقائق |
| نجاح إكمال الدرس | > 99.9% | 7 أيام | — | < 99% لـ 15 دقيقة |
| زمن بدء الوظائف (p95) | < 30s | 24 ساعة | — | Lag > 60s |
| DLQ | 0 رسالة | مستمر | — | أي رسالة |
| RPO / RTO | ≤ 5 دقائق / ≤ 60 دقيقة | — | — | فشل اختبار استعادة |

**سياسة ميزانية الخطأ:** إذا استُهلك 50% من الميزانية الشهرية في الأسبوع الأول، **يتوقف إدخال الميزات** ويُوجَّه الفريق للاستقرار حتى تعود الميزانية.

---

## 6) المعمارية المستهدفة

### 6.1 المخطط
```
                       ┌────────────────────────┐
   التطبيق ───────────▶│  CDN + WAF + DDoS     │───▶ Object Storage (S3/R2)
        │              └──────────┬─────────────┘        (media, immutable)
        │ dynamic                 │ dynamic
        ▼                         ▼
   ┌───────────────────────────────────────────┐
   │ Load Balancer (TLS, HTTP/2, Health)       │
   └───────┬───────────────────────┬───────────┘
           │                       │
     ┌─────▼─────┐  ...      ┌─────▼─────┐      N × API pods (stateless)
     │ API pod 1 │           │ API pod N │
     └─────┬─────┘           └─────┬─────┘
           └──────────┬────────────┘
                      ▼
              ┌───────────────┐          ┌─────────────────────┐
              │  PgBouncer    │          │ Redis Cache (HA)    │
              │ transaction   │          │ allkeys-lru         │
              └───────┬───────┘          └─────────────────────┘
                      │                  ┌─────────────────────┐
        ┌─────────────┴─────────┐        │ Redis Queue (HA)    │
        ▼                       ▼        │ noeviction / Streams│
  ┌───────────┐   WAL     ┌────────────┐ └──────────┬──────────┘
  │ PG Primary│──────────▶│ PG Replica │            │
  └───────────┘           └────────────┘            ▼
        │                                      ┌───────────────┐
        └──────────────────────────────────────│ Worker pool   │
                                               │ 20–50 conc.   │
                                               └───────┬───────┘
                                                       ▼
                                               Push / Email / Analytics
```

### 6.2 مبادئ التصميم
1. **API بلا حالة** — كل شيء في PostgreSQL/Redis/Object Storage، لا ملفات محلية.
2. **مستوى المحتوى ≠ مستوى المتعلّم** — الكتالوج عام ويُكاش بقوة (CDN + Redis)، بينما التقدم/الجلسات خاصة ولا تُكاش إلا لكل مستخدم.
3. **الكتابة الحرجة متزامنة** — XP، streak، إكمال الدرس تبقى داخل معاملة واحدة.
4. **كل ما ليس حرجاً يصبح حدثاً** — الإشعارات، التحليلات، البريد، تحديثات الترتيب → Outbox + Worker.
5. **فشل متدرّج** — تعطّل AI/Redis/الطابور لا يجب أن يمنع درساً.
6. **قابلية الملاحظة جزء من التعريف** — أي endpoint جديد يجب أن يصدّر Metrics.

### 6.3 تدفّقات حرجة

**أ. فتح التطبيق**
```
GET /content/tree
  → CDN hit؟ نعم: استجابة فورية
  → لا: Redis hit؟ نعم: JSON جاهز
  → لا: بناء الشجرة (استعلامات مجمّعة) + SETEX + Single-Flight
  → CDN يخزّن 5 دقائق، العميل يخزّن مع ETag
```

**ب. الدرس**
```
POST /quiz/sessions        (كتابة: جلسة + تقدم)
POST /quiz/sessions/{id}   × N (كل إجابة معاملة قصيرة)
POST /quiz/sessions/{id}   (complete: معاملة واحدة تشمل XP/streak/achievements/daily)
GET  /progress             (Cache 60s أو عدّادات)
```

**ج. إكمال الدرس (Fan-out)**
```
BEGIN
  تحديث session + progress + user_stats + streak + daily challenge
  كتابة outbox_event(lesson.completed)
COMMIT
  → Worker: إشعارات، تحليلات، تحديث لوحة الترتيب
```

**د. AI**
```
BEGIN (قراءة السياق فقط) → COMMIT/إغلاق
نداء المزوّد مع Timeout 8s + Semaphore + Circuit Breaker
BEGIN (كتابة المحادثة + usage) → COMMIT
```

---

## 7) مسارات العمل التفصيلية

### 7.1 الحافة والوسائط (Edge & Media)
**الحالة:** لا CDN، لا WAF، الوسائط من الحاوية.
**المطلوب:**
- CDN أمام كل شيء. قواعد:
  - `/api/v1/content/*`: `Cache-Control: public, s-maxage=300, stale-while-revalidate=3600`.
  - `/media/*`: `public, max-age=31536000, immutable` (أسماء بإصدار/hash).
  - `/api/v1/auth/*`, `/api/v1/me/*`, `/api/v1/quiz/*`, `/api/v1/progress/*`: **Bypass cache**.
- WAF: قواعد OWASP + Bot + Rate limit عام لكل IP بحد **مرتفع** (مثلاً 1,000 req/s) لأن CGNAT يجمع آلاف المستخدمين على عنوان واحد؛ الحماية الدقيقة تكون لكل مستخدم داخل التطبيق (F1).
- ضغط عند الحافة (Brotli) وإلغاء GZip من التطبيق إذا كان الـ CDN يدعمه.
- الوسائط: WebP/AVIF للصور (≤ 60KB)، Opus mono 32–48kbps للصوت (≤ 150KB/دقيقة)، HLS للفيديو إن وُجد.
- Signed URLs للمحتوى المدفوع فقط، وليس للمنهج العام.
**معيار القبول:** ≥ 95% من طلبات الوسائط تُخدم من CDN، وEgress الأصلي < 5%.

### 7.2 طبقة الـ API
- **نسخة واحدة من uvicorn لكل حاوية** (لا `--workers`)، والتوسع بالنسخ. كل عامل إضافي يضاعف pool القاعدة بلا داعٍ.
- نقاط النشر:
```bash
uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips="10.0.0.0/8" \
  --limit-concurrency 400 --backlog 2048 \
  --timeout-keep-alive 15 \
  --timeout-graceful-shutdown 25
```
- Autoscaling: CPU 60% + RPS/نسخة 400 + p95. الحد الأدنى 3 نسخ (N+1 عبر AZ).
- **Graceful Shutdown:** أوقف قبول الاتصالات، اسمح 25 ثانية للطلبات الجارية، ثم أغلق الـ pool. تأكد من `STOPSIGNAL SIGTERM` (موجود ضمنياً).
- **Payload budget:** أقل من 50KB لكل شاشة؛ أزل الحقول غير المستخدمة؛ استخدم `orjson` إذا أثبت القياس أن التسلسل عنق زجاجة.
- **HTTP/2 وKeep-Alive** على الـ LB لتقليل handshakes من الموبايل.

### 7.3 قاعدة البيانات
#### أ. ضبط الـ Pool
```python
# لكل نسخة API — القاعدة: 120 اتصالاً أساسياً + Overflow محدود
pool_size = max(4, 120 // api_replicas)
max_overflow = 3
pool_timeout = 2          # لا 30
pool_recycle = 1800
pool_pre_ping = True
```
> مع 10 نسخ: 12 × 10 = 120 أساسياً + 30 Overflow = 150 كحد أقصى، وهو داخل السقف الآمن.
- استخدم **PgBouncer** بوضع `transaction` بين التطبيق والقاعدة.
- **مهم:** `asyncpg` يستخدم Prepared Statements، وPgBouncer في وضع transaction يحتاج `max_prepared_statements` (1.21+) أو `statement_cache_size=0` من جهة العميل (بأداء أقل). اختر واختبر.
```ini
[pgbouncer]
pool_mode = transaction
max_client_conn = 2000
default_pool_size = 100
min_pool_size = 10
reserve_pool_size = 20
reserve_pool_timeout = 3
query_wait_timeout = 5
server_idle_timeout = 60
```
- `max_connections` في PostgreSQL: 300–400 (مع PgBouncer)، و`superuser_reserved_connections=5`.

#### ب. Timeouts
ابدأ بـ `statement_timeout=4s` للـ API و`30s` للـ worker (وظائف ثقيلة)، و`idle_in_transaction_session_timeout=8s`.
> لا تضع `statement_timeout` صارماً على migration أو تقارير admin؛ افصلها بدور/اتصال مختلف.

#### ج. الفهارس المطلوبة (أمثلة)
```sql
-- يخدم ملخص التقدم: كل الجلسات المكتملة لمستخدم مرتبة بالزمن
CREATE INDEX CONCURRENTLY ix_quiz_sessions_user_completed
  ON quiz_sessions (user_id, completed_at DESC)
  WHERE status = 'completed';

-- يخدم لوحة الإدارة ومراجعة الجودة دون مسح كامل
CREATE INDEX CONCURRENTLY ix_quiz_answers_question_created
  ON quiz_answers (question_id, created_at DESC);

-- لا تضف فهرساً جديداً للـ Purge: `ix_refresh_expires` موجود ويخدمه.
-- راجع pg_stat_user_indexes أولاً؛ كل فهرس مكرر هو تكلفة كتابة خالصة.
```
> راجع `pg_stat_user_indexes` شهرياً؛ كل فهرس غير مستخدم هو تكلفة كتابة خالصة.

#### د. إصلاح الاستعلامات
1. **`tree()`**: استعلامات مجمّعة بـ `JOIN` واحد أو 3 استعلامات (levels, units, lessons) ثم التجميع في الذاكرة، بدل 1+L+U.
2. **`progress.summary`**: عدّادات في `user_stats` (`answered_total`, `correct_total`, `words_learned`, `lessons_completed`) تُحدَّث في معاملة الإكمال، مع Backfill لمرة واحدة.
3. **`unlock_map` / `_assert_lesson_unlocked`**: احسب الخريطة مرة لكل مستخدم وخزّنها 60s، وأبطلها عند الإكمال.
4. **Admin stats**: جدول `daily_metrics` يُحدَّث ليلاً + استعلامات نافذة زمنية، لا `COUNT(*)` على كل التاريخ.

#### هـ. Read Replicas
- استخدمها فقط لما يتحمل تأخيراً: `admin stats`, `ai usage reports`, تحليلات، والكتالوج العام (شُحن الآن: قراءات الكتالوج فقط).
- **لا** تستخدمها لـ: التقدم بعد الإكمال، الجلسات، المصادقة، أو أي قراءة بعد كتابة (read-your-writes).
- وفّر `uow_factory(read_only=True)` ومساراً واضحاً في `deps.py`، وليس قراراً ضمنياً. **الشحن الحالي:** `DATABASE_READ_URL` + `make_read_uow()` و`ReadUowDep`، والكتالوج وحده يستخدمه؛ غياب المتغير أو تساوي الـDSN يعيد محرك الأساس نفسه (لا pool ثانٍ).
- راقب `replay_lag`؛ إذا > 5 ثوانٍ، حوّل القراءات للـ Primary تلقائياً. **لم يُشحن بعد:** قياس الـlag والتحويل التلقائي؛ يبقى `pg_replication_lag_seconds` من F6 هو المدخل.

#### و. Migrations
- Expand/Contract كما في F3.
- `CREATE INDEX CONCURRENTLY` خارج المعاملة.
- `ALTER TABLE ... ADD COLUMN` مع Default في PostgreSQL 11+ آمن نسبياً، لكن `SET NOT NULL` على جدول كبير يحتاج Validation منفصلة.
- اختبر كل migration على نسخة بحجم الإنتاج.

#### ز. الصيانة
- `autovacuum` مضبوط للجداول الكبيرة: `autovacuum_vacuum_scale_factor=0.02`, `autovacuum_analyze_scale_factor=0.01`.
- `pg_stat_statements` مفعّل.
- مراقبة Bloat وإعادة الفهرسة (`REINDEX CONCURRENTLY`) عند الحاجة.

### 7.4 الكاش
**الطبقات:**
| الطبقة | ماذا | TTL |
|---|---|---|
| العميل (Flutter) | الكتالوج، الدروس | ETag + 5–15 دقيقة |
| CDN | `/content/tree`, `/content/lessons/*`, `/media/*` | 5 دقائق + SWR |
| Redis | شجرة الكتالوج، درس كامل، ملخص تقدم | 5–15 دقيقة / 60s |
| In-process | إعدادات، خريطة رموز | 30–60s (حذر مع نسخ متعددة) |

**Cache-Aside مع Single-Flight:**
```python
async def get_tree(version: str, attempt: int = 3) -> Tree:
    key = f"content:tree:{version}"
    if raw := await redis.get(key):
        return Tree.model_validate_json(raw)
    # قفل قصير يمنع موجة إعادة البناء
    if await redis.set(key + ":lock", "1", nx=True, ex=10):
        try:
            tree = await build_tree_from_db()
            await redis.set(key, tree.model_dump_json(),
                            ex=900 + random.randint(0, 120))
            return tree
        finally:
            await redis.delete(key + ":lock")
    await asyncio.sleep(0.05)
    # مع حد أقصى للمحاولات (مثلاً 3) ثم البناء مباشرة، وإلا صار القفل سبباً
    # لحلقة لا تنتهي إذا فشل الباني.
    return await get_tree(version, attempt=3)  # أو stale copy إن وجدت
```
**الإبطال:** عند publish/unpublish من لوحة الإدارة: `INCR content:version` + CDN purge للمسار. لا تحذف مفاتيح فردية.
**قواعد:**
- لا تُكاش أي بيانات مستخدم في كاش مشترك.
- أضف Jitter دائماً لمنع التزامن.
- راقب hit ratio؛ الهدف > 90% للكتالوج.
- استخدم `stale-while-revalidate` حتى لا يرى المستخدم Spinner أثناء إعادة البناء.

### 7.5 الحدود وسوء الاستخدام
- **Token Bucket** Lua (ذرّي):
```lua
-- KEYS[1]=bucket  ARGV: capacity, refill_per_sec, now_ms, cost
local cap, refill = tonumber(ARGV[1]), tonumber(ARGV[2])
local now, cost = tonumber(ARGV[3]), tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - ts) / 1000 * refill)
if tokens < cost then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('PEXPIRE', KEYS[1], math.ceil(cap / refill * 1000) + 1000)
  return {0, math.ceil((cost - tokens) / refill)}
end
redis.call('HSET', KEYS[1], 'tokens', tokens - cost, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / refill * 1000) + 1000)
return {1, 0}
```
- المفتاح: `user:{id}` أو `token:{sha256}` أو `ip:{addr}`.
- الحدود المقترحة (لكل مستخدم):
  - قراءات كتالوج: 600/دقيقة.
  - إجابات: 240/دقيقة.
  - AI: 20/دقيقة + سقف يومي.
  - مصادقة: 10/دقيقة لكل IP + 5/دقيقة لكل حساب (لمنع Credential Stuffing).
- أعد `Retry-After` دقيقاً (من الـ Lua)، وليس 60 ثابتاً.
- راقب `429` حسب النوع؛ إذا تجاوز 0.5% للمستخدمين الموثّقين فهناك خطأ في التحديد.

### 7.6 الطابور والعمل غير المتزامن
**المشحون الآن (Phase 0):** `BLMOVE` ينقل الوظيفة ذرياً من `jobs` إلى `jobs:processing`، مع `ZADD` لموعد انتهاء الصلاحية (300s). العامل ينادي `ack()` بعد النجاح أو بعد استنفاد المحاولات، و`recover()` يفحص كل 30 ثانية ويعيد الوظائف المنتهية إلى `jobs`. هذا **at-least-once**: القتل في منتصف الوظيفة يعيدها، لكنه قد ينفّذها مرتين — لذلك كل معالج يجب أن يكون idempotent. لا DLQ دائم بعد.

**المعمارية المقترحة (Phase 1):**
```
Stream: jobs:default   Consumer Group: workers
Stream: jobs:notify    Consumer Group: workers
Stream: jobs:analytics Consumer Group: workers
Stream: jobs:dlq
```
**خصائص إلزامية:**
- `XACK` بعد النجاح فقط.
- `XAUTOCLAIM` لاستعادة الرسائل المعلقة من عامل ميت.
- `max_attempts` + backoff أُسّي مع Jitter.
- DLQ بعد الاستنفاد، مع أدوات إعادة تشغيل.
- Concurrency داخل العملية (Semaphore) وليس مستهلكاً واحداً.
- Handler Idempotent (مفتاح العملية).
- مراقبة: `lag`, `pending`, `dlq_size`, `job_duration`.

**Outbox:**
```sql
CREATE TABLE outbox_events (
  id           uuid PRIMARY KEY,
  event_type   text NOT NULL,
  payload      jsonb NOT NULL,
  occurred_at  timestamptz NOT NULL DEFAULT now(),
  available_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  attempts     int NOT NULL DEFAULT 0,
  last_error   text
);
CREATE INDEX ix_outbox_unpublished
  ON outbox_events (available_at) WHERE published_at IS NULL;
```
- النشر داخل نفس معاملة الحدث (مثلاً إكمال الدرس).
- Relay Worker يقرأ بـ `FOR UPDATE SKIP LOCKED` وينشر، ثم يعلّم `published_at`.
- هذا يمنع “حدث في القاعدة بلا وظيفة” أو العكس.

**جدولة:**
- لا تضع الـ Scheduler داخل كل Worker بلا قفل. استخدم قائداً منتخباً (`pg_advisory_lock` أو Redis lock) أو Cron خارجي (EventBridge/CronJob).
- الوظائف الليلية: Purge، Rollups، Partition maintenance، إعادة احتساب الترتيب.

### 7.7 مسار الكتابة والاتساق
- **إكمال الدرس**: معاملة واحدة تشمل `quiz_sessions`, `lesson_progress`, `user_stats`, `user_achievements`, `daily_challenge`, `outbox`. حجمها ~10–15 استعلاماً؛ حافظ عليها < 100ms.
- **الترتيب (Locking)**: لا `SELECT ... FOR UPDATE` على صفوف مشتركة (مثل لوحة الترتيب) داخل مسار الدرس. استخدم `INSERT ... ON CONFLICT DO UPDATE` على صف المستخدم فقط.
- **Idempotency**: `(user_id, session_id)` فريد في المساهمات اليومية يمنع الاحتساب المزدوج — ممتاز، حافظ عليه.
- **الاتساق النهائي**: الإشعارات والتحليلات قد تتأخر ثوانيَ؛ لا تعرضها كجزء من نتيجة الدرس. أضف `eventual` في واجهة الإدارة إذا لزم.
- **التعارضات**: استخدم `version` أو شروط `WHERE updated_at = ...` للـ PATCH على الملف الشخصي إذا تعددت الأجهزة.

### 7.8 المصادقة والهوية
- Access Token بلا حالة: جيد. لكن كل طلب موثّق يقرأ المستخدم من القاعدة (حتى مع `lazy="joined"`).
  - الحل: Cache قصير (5s) لحالة المستخدم في Redis/In-process، مع إبطال فوري عند الحظر/الحذف. **لا تخزّن `is_admin` في التوكن**.
- Refresh:
  - ارفع TTL إلى 30 دقيقة لتقليل الكتابات إلى النصف.
  - أبقِ Reuse Detection (ممتاز).
  - Purge على دفعات.
  - أضف مؤشر `family_id` (موجود) ومراقبة عدد العائلات النشطة.
- المفاتيح: انتقل إلى RS256/EdDSA + `kid` قبل أي خدمة ثانية، ووثّق إجراء التدوير.
- الأجهزة: `user_devices` جيد؛ أضف حداً أقصى للأجهزة (مثلاً 10) وإبطال الأقدم.
- Dev Login: محظور في الإنتاج — ممتاز. أبقِ الاختبار يثبت ذلك.

### 7.9 الذكاء الاصطناعي
- **ليس مساراً حرجاً**: Fallback موجود، حافظ عليه.
- الحدود:
  - `AI_DAILY_USER_BUDGET_USD` (موجود) + **سقف عالمي يومي** (مثلاً $X) + سقف لكل feature.
  - حد أقصى للتوكنات والطول (موجود).
- الحماية:
  - Semaphore (Bulkhead) 20–40 نداءً متزامناً.
  - Circuit Breaker.
  - Timeout 8–10s.
  - Queue + Polling للطلبات الطويلة (أو Streaming عبر SSE).
- التكلفة:
  - Metering مجمّع (Buffer + flush كل 5s أو 100 سجل).
  - لوحة تكلفة يومية/feature/نموذج.
  - Cache للأسئلة الشائعة (Exact match hash).
- الخصوصية: لا ترسل PII إلى المزوّد؛ جرّد الرسائل من البريد/المعرّفات.

### 7.10 المراقبة والتشغيل
**Metrics (Prometheus):**
- HTTP: `http_requests_total{route,method,status}`, `http_request_duration_seconds_bucket{route}`.
- DB: `db_pool_in_use`, `db_pool_waiters`, `db_query_duration_seconds`, `db_connections`, `pg_replication_lag_seconds`.
- Redis: `redis_commands_total`, `redis_memory_used_bytes`, `redis_evicted_keys_total`, `cache_hits_total`, `cache_misses_total`.
- Queue: `queue_depth`, `queue_lag_seconds`, `queue_pending`, `dlq_size`.
- Business: `lessons_completed_total`, `answers_graded_total`, `users_active_daily` (Gauge يُحدَّث دورياً، لا Counter بلا حدود).
- AI: `ai_requests_total{feature,status}`, `ai_cost_usd_total`, `ai_fallback_total`.

**Traces:** OTel Collector → Tempo/Jaeger. عينة 10%، و100% للأخطاء و`p99` (Tail sampling).
**Logs:** JSON إلى stdout → Vector/Fluent Bit. عينة 1:10 للطلبات الناجحة، 100% للأخطاء و4xx المهمة. لا تسجيل Tokens/PII.
**Sentry:** `sentry_sdk.init(dsn=..., traces_sample_rate=0.05, environment=...)`.
**Dashboards:**
1. Golden Signals (RPS, Errors, p50/p95/p99, Saturation).
2. Database (Connections, TPS, Slow queries, Replication lag, Bloat).
3. Cache & Queue.
4. Business funnel (Sign-in → Lesson start → Complete).
5. AI cost & quality.
**Alerts (أمثلة):**
```promql
# نسبة 5xx
sum(rate(http_requests_total{status=~"5.."}[5m]))
/ sum(rate(http_requests_total[5m])) > 0.02

# p95
histogram_quantile(0.95,
  sum by (le, route)(rate(http_request_duration_seconds_bucket[5m]))) > 0.3

# pool مشبع
db_pool_in_use / (db_pool_size + db_pool_overflow) > 0.85

# Redis يحذف مفاتيح الطابور
increase(redis_evicted_keys_total{instance="queue"}[5m]) > 0
```
**Runbooks:** لكل تنبيه خطوات، مالك، وRollback.

### 7.11 الأمان والامتثال
- Secret Manager؛ لا `.env` على الخادم. تدوير دوري.
- TLS 1.2+، HSTS (موجود)، شهادات آلية.
- WAF + DDoS + Bot.
- مبدأ أقل صلاحية: مستخدم DB للتطبيق لا يملك `SUPERUSER`، ولا `DDL` في الإنتاج.
- تشفير at-rest (Managed PG/S3) وin-transit.
- تصغير PII: البريد فقط، لا أرقام هواتف إلا لحاجة.
- سجلات تدقيق غير قابلة للتعديل (موجودة بنيوياً؛ أضف Partition + Retention).
- فحص التبعيات (`pip-audit`) والصور (`Trivy`) وSBOM في CI.
- اختبار اختراق سنوي، ومراجعة صلاحيات admin ربع سنوية.

### 7.12 التوافر والتعافي (DR)
- **RPO ≤ 5 دقائق، RTO ≤ 60 دقيقة.**
- نسخ احتياطي: يومي كامل + WAL مستمر + نسخة أسبوعية عبر المنطقة.
- اختبار استعادة شهري (Automated) إلى بيئة معزولة + تقرير.
- Failover آلي للـ Primary (Managed HA أو Patroni).
- Runbook لتعطّل Redis: النظام يجب أن يعمل degraded (بلا Cache/Queue) لا أن يتوقف — وهذا يتطلب إصلاح F32 أولاً، وإلا فالسطر الحالي غير صحيح.
- Runbook لتعطّل AI: Fallback فوري.
- احتفظ بآخر 3 صور Docker قابلة للنشر للـ Rollback.

### 7.13 العقد مع تطبيق الموبايل
- Backoff أُسّي + Jitter، واحترام `Retry-After`.
- `Idempotency-Key` لكل كتابة (UUID جديد لكل عملية منطقية).
- `If-None-Match` للكتالوج، وتخزين محلي.
- حدود حجم: < 50KB/شاشة، صور مشفرة، تحميل مسبق للدرس التالي.
- قائمة كتابات غير مُرسلة (Offline Queue) تُعاد عند الاتصال — الخادم Idempotent.
- لا تعِد المحاولة على 4xx (إلا 429).
- تمييز `degraded` في الاستجابة عند استخدام Fallback للـ AI (موجود في الـ Response).

---

## 8) تغييرات ملموسة في الكود (Before → After)

> كل المقتطفات التالية **مقترحة** وليست وصفاً للكود الحالي؛ بعضها يستدعي واجهات جديدة (مثل `cache.token_bucket`) يجب إضافتها إلى الـ Port أولاً.

### 8.1 مفتاح الحدود
```python
# before: scope + client_ip + window
key = cache.build_key("ratelimit", scope, client_ip(request), str(window))

# after: المستخدم أولاً، ثم بصمة التوكن، ثم IP
principal = (
    getattr(request.state, "user_id", None)
    or token_fingerprint(request)     # sha256 للتوكن، غير قابل للتزوير
    or f"ip:{client_ip(request)}"
)
allowed, retry_after = await cache.token_bucket(
    key=cache.build_key("rl", scope, principal),
    capacity=budget, refill_per_second=budget / 60, cost=1,
)
if not allowed:
    raise RateLimitedError(..., retry_after=retry_after)
```

### 8.2 التحقق من المستخدم
```python
# before: قراءة DB في كل طلب
user = await self._uow.users.get(UserId(claims.subject))

# after (مقترح): كاش قصير مع إبطال عند الحظر/الحذف
user = await self._users_cache.get_or_load(
    claims.subject, ttl=5,
    loader=lambda: self._uow.users.get(UserId(claims.subject)),
)
```

### 8.3 AI بلا احتجاز اتصال
```python
# 1) سياق قصير
async with session_scope() as session:
    context = await load_context(session, user_id)
# 2) نداء خارجي بلا جلسة
async with ai_bulkhead:
    reply = await ai.tutor_reply(context=context, ...)
# 3) كتابة قصيرة
async with session_scope() as session:
    await persist_turn(session, user_id, conversation_id, text, reply)
```

### 8.4 الكتالوج
```python
# before: 1 + L + U استعلاماً في كل طلب
level_page = await self.list_levels(limit=MAX_PAGE_SIZE)
for level in level_page.items:
    unit_page = await self.list_units(level.code, limit=MAX_PAGE_SIZE)
    for unit in unit_page.items:
        await self.list_lessons(unit.id, limit=MAX_PAGE_SIZE)

# after: Redis + بناء مجمّع
tree = await self._cache.get_or_build(
    key=f"content:tree:{await self._version()}",
    ttl=900, jitter=120,
    builder=self._build_tree_with_three_queries,
)
```

### 8.5 الـ Migration (Docker)
```yaml
# docker-compose.yml (مختصر)
services:
  migrate:
    image: ghcr.io/natiqo/api:${TAG}
    command: ["alembic", "upgrade", "head"]
    restart: "no"
    depends_on:
      db: {condition: service_healthy}
  app:
    image: ghcr.io/natiqo/api:${TAG}
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
    deploy:
      replicas: 3
```

### 8.6 الطابور
```python
# before
await redis.rpush("natiqo:jobs", payload)
job = await redis.blpop(["natiqo:jobs"], timeout=1)

# after (Redis Streams)
await redis.xadd("jobs:default", "*", **job_fields)
msgs = await redis.xreadgroup(
    groupname="workers", consumername=consumer_id,
    streams={"jobs:default": ">"}, count=10, block=5000,
)
# ... نفّذ ... ثم:
await redis.xack("jobs:default", "workers", msg_id)
# استعادة العالق:
await redis.xautoclaim("jobs:default", "workers", consumer_id,
                       min_idle_time=60_000, start_id="0-0")
```

### 8.7 الفهارس والعدّادات
```sql
ALTER TABLE user_stats
  ADD COLUMN answered_total   integer NOT NULL DEFAULT 0,
  ADD COLUMN correct_total    integer NOT NULL DEFAULT 0,
  ADD COLUMN lessons_completed integer NOT NULL DEFAULT 0,
  ADD COLUMN words_learned_total integer NOT NULL DEFAULT 0;
-- Backfill لمرة واحدة في migration منفصلة (وليس في نفس النشر)
```
> طبّق Expand/Contract: أضف الأعمدة → Backfill في Job → اقرأ منها → احذف الاستعلامات القديمة.

---

## 9) خطة التنفيذ المرحلية

### Phase 0 — نرى ونحمي (أسبوعان، ~18 يوم-مهندس)
**الهدف:** معرفة الوضع الحقيقي وإزالة المخاطر القاتلة قبل أي تحسين كبير.

| المهمة | التفاصيل | معيار القبول |
|---|---|---|
| P0-1 Observability | `/metrics`, OTel traces, Sentry, 3 Dashboards, 8+ Alerts | كل طلب قابل للتتبع |
| P0-2 Rate limiting | Principal = User/Token، Token Bucket، Proxy headers | 1,000 VU من IP واحد → 429 < 1% |
| P0-3 AI isolation | فصل الجلسة عن النداء، Bulkhead، Timeout 10s | Pool < 40% تحت 500 طلب AI |
| P0-4 Migrations | Job منفصل + Advisory Lock | Rolling deploy بلا أقفال |
| P0-5 Backup/DR | PITR + استعادة مُختبَرة + Runbook | استعادة < 60 دقيقة |
| P0-6 Timeouts | statement/lock/pool + uvicorn limits | 3× حمل → 503 سريع لا تعليق |
| P0-7 Load baseline | k6 scripts + تقرير Baseline | 200 RPS، p95 موثق |
| P0-8 Queue safety | Redis منفصل + noeviction + استرجاع | لا فقدان وظائف عند القتل |
| P0-9 Redis failure mode | Fail-open + Fallback + Readiness degraded | قتل Redis 60s → 0 أخطاء 5xx |

**مخرجات:** تقرير Baseline، Dashboards، Runbooks، backlog محدث بأرقام حقيقية.

### 9.1 حالة التنفيذ الفعلية (2026-10-31)

شُحن في هذه الدفعة **8 بنود P0 كاملة + بند واحد جزئي (F6)** إضافة إلى F10/F11/F25 وبداية P2-1، وأُضيف 103 اختباراً جديداً (المجموع 1,090 اختباراً، والتغطية 83.2%، والحد الأدنى في CI هو 80%). كل بند أدناه يشير إلى مكانه في الكود، حتى لا تتحول الخطة إلى وثيقة نوايا.

| البند | الحالة | ما شُحن فعلاً | المتبقي / القيد |
|---|---|---|---|
| **F1** حدود لكل هوية | ✅ مكتمل | `rate_limit_principal()` في `app/api/deps.py`: user id ← `t:<sha256(token)>` ← IP. مفتاح الحد لا يحتوي التوكن الخام. `--proxy-headers` و`--forwarded-allow-ips` في `Dockerfile`. رُفع سقف auth إلى 120/دقيقة والكتالوج إلى 1,200/دقيقة لاستيعاب CGNAT. | لا شيء برمجياً؛ يبقى ضبط الأرقام من نتائج الحمل. |
| **F32** Redis ليس نقطة فشل قاتلة | ✅ مكتمل | `increment` يسقط إلى عدّاد داخل العملية عند فشل Redis (fail-open مع حدّ تقريبي)، `IdempotencyMiddleware._load` يعيد `None` بدل 500، و`overall_status()` في `app/core/health.py` يعتبر PostgreSQL الوحيد القاتل؛ Redis/queue ⇒ `DEGRADED` وHTTP 200. | اختبار قتل Redis 60 ثانية على 500 RPS (يحتاج بيئة staging). |
| **F2** عزل الذكاء الاصطناعي | ✅ مكتمل | `AiTutorService.explain/ask` ينهي المعاملة **قبل** نداء المزوّد (البيانات تُنسخ إلى locals)، و`AIService._call_provider` يفرض `asyncio.Semaphore` (bulkhead) + `wait_for` بحد 10 ثوانٍ؛ التشبع/المهلة يسقطان إلى الرد المؤلَّف لا إلى 500. | Circuit breaker عالمي وMetering مجمّع (P2-4). |
| **F3** الترحيلات | ✅ مكتمل | خدمة `migrate` مستقلة في `docker-compose.yml`، و`CMD` في `Dockerfile` لم يعد يشغّل `alembic upgrade head`، و`alembic/env.py` يأخذ `pg_advisory_xact_lock` داخل معاملة الترحيل. | Expand/Contract يبقى قاعدة مراجعة لكل migration جديدة. |
| **F7** مهلات قاعدة البيانات | ✅ مكتمل | `statement_timeout=4s`, `lock_timeout=1.5s`, `idle_in_transaction_session_timeout=8s` تُمرَّر عبر `connect_args.server_settings`، و`database_pool_timeout` من 30 إلى 3 ثوانٍ. | لا شيء. |
| **F6** المراقبة | ⚠️ جزئي | `GET /metrics` (خارج نسخة الـ API) يعرض: عدد الطلبات حسب route/status، histogram للكمون، in-flight، uptime، حجم/إشغال pool قاعدة البيانات، وعمق الطابور. أُضيف `deploy/monitoring/`: إعداد Prometheus، **10 قواعد تنبيه**، ولوحة Grafana جاهزة للاستيراد بثلاثة أقسام. | **لم يُشحن بعد:** Sentry، OTel traces. `SENTRY_DSN`/`OTEL_*` ما زالت غير موصولة وتحتاج SDK + `uv.lock`؛ ولا يمكن إغلاق F6 قبل trace واحد من الـ ingress إلى SQL. |
| **F4** أمان الطابور | ✅ مكتمل (Phase 0) | Redis منفصل للطابور (`QUEUE_REDIS_URL`) بسياسة `noeviction` مع volume، وRedis للكاش بـ `allkeys-lru`. الاستهلاك أصبح **at-least-once**: `BLMOVE` إلى processing list + `ack()` + `recover()` لاسترجاع الـ claims المنتهية (visibility 300s) كل 30 ثانية؛ والمهمة السامة تُـack وتُسجَّل بدل أن تدور للأبد. | التكرار وارد بحكم at-least-once. الوظيفة المستنفدة تُسجَّل الآن في dead-letter list محدودة (10k) قبل ack. يبقى Streams + XAUTOCLAIM لإعادة التشغيل الآلي في P1-6. |
| **F5** النسخ والتعافي | ❌ خارج الكود | — | PITR على PostgreSQL المُدار + تمرين استعادة شهري مكتوب في Runbook. لا يمكن إنجازه من داخل المستودع. |
| **F28** كاش الوسائط | ✅ مكتمل | `/media/*` يحصل على `Cache-Control: public, max-age=604800, stale-while-revalidate=86400` و`Cross-Origin-Resource-Policy: cross-origin`؛ الملف المفقود يبقى `no-store`. | إعداد CDN/WAF (P1-1, P1-5) خارج الكود. |
| **F29** قفل Idempotency | ✅ مكتمل | قفل in-flight عبر `increment(key:inflight)` بعمر 60s: الطلب المتزامن الثاني لنفس المفتاح يحصل على `409 IDEMPOTENCY_IN_PROGRESS` + `Retry-After: 1` بدل تنفيذ الكتابة مرتين. القفل يُحرَّر بعد تخزين الرد، وفشل الكاش يجعله fail-open. | لا شيء. |

**تحسينات إضافية شُحنت مع هذه الدفعة:**

- **F8:** `CatalogueService.tree()` لم يعد يبني الشجرة باستدعاءات متسلسلة؛ يقرأ levels/units/lessons/عدد الأسئلة في **4 استعلامات ثابتة** ثم يجمّعها في الذاكرة (كان ~37 على المنهج المزروع). وأُضيف **كاش مشترك للشجرة** (Redis في الإنتاج، TTL = `CATALOGUE_CACHE_TTL_SECONDS` = 30s) مع تعامل fail-open: أي عطل أو payload تالف يسقط إلى قاعدة البيانات بدل 500. أي كتابة إدارية تحت `/admin/content` تُبطل المفتاح بعد الاستجابة (dependency على الـrouter، لا على كل method)، فالنشر يظهر في الطلب التالي مباشرة؛ يبقى CDN/Edge في P1-4.
- **F30:** إدراج الإجابة أصبح `INSERT ... ON CONFLICT (session_id, question_id) DO NOTHING`. الطلب الخاسر في سباق إجابتين يقرأ إجابة الفائز ويعيد حكمها بدل `IntegrityError` → 500، ولا يضيف عدّاً ثانياً للإجابة الصحيحة.
- **F32 (المتبقي):** كتابة `user_stats` صارت تحت قفل صف (`SELECT ... FOR UPDATE`) في إتمام الدرس والمطالبة بالتحدي وتجميد السلسلة، مع `ON CONFLICT DO NOTHING` لإدراج أول إنجاز. إتمامان متزامنان لم يكن أحدهما يضيّع XP الآخر بصمت.
- **F13 (جزء منه):** سقف `AI_MAX_CONCURRENT_REQUESTS` كان يُنشأ داخل خدمة لكل طلب، أي أنه لم يكن يحدّ شيئاً؛ صار semaphore مشتركاً لكل event loop.
- **F18 (جزء منه):** لا تُخزَّن استجابة idempotency أكبر من 64KB، فلا يزيح تصديرٌ ضخم مفاتيح الكاش الصغيرة لليوم كله.
- **F31:** حذف سؤال له تاريخ إجابات صار مرفوضاً بـ409 بدل أن يمحو `quiz_answers` عبر `ON DELETE CASCADE`؛ المعلم يُبقي الدرس غير منشور لإخفائه. الفحص على مستوى قاعدة البيانات (exists) لا على حالة الدرس وحدها.
- **F4/P1-6 (جزء):** `QueuePort.dead_letter` يحفظ الوظيفة المستنفدة (الاسم، المعرف، آخر خطأ) في قائمة Redis محدودة بـ10,000 قبل ack، وفي المحول داخل العملية. لم تعد الأعطال الدائمة تعيش في سجل قد يُدار فقط.
- **F16/P1-2:** أُضيف `docker-compose.pooled.yml` (PgBouncer، transaction mode، حدود وصف واضحة) و`deploy/pgbouncer/README.ar.md` بحساب الأحجام وأوامر التحقق والتراجع؛ الترحيلات تبقى على الاتصال المباشر. **لم يُشغَّل داخل هذه البيئة** (لا Docker) ويحتاج تحقق staging.
- **F20:** أُضيف `.github/workflows/loadtest.yml`: تشغيل أسبوعي/يدوي على PostgreSQL وRedis حقيقيين، ترحيل + seed + توكنات لكل VU + k6 مع رفع summary؛ الحدود الافتراضية محافظة وليست اختبار سعة كامل.
- **F17/P1-7 (جزء):** access log صار قابلًا للـsampling عبر `ACCESS_LOG_SAMPLE_RATE` (افتراضي 1.0) مع استثناء دائم لكل 5xx وكل طلب أبطأ من `ACCESS_LOG_SLOW_MS`؛ المقاييس تبقى تعدّ كل طلب. كاش `/ready` لثانيتين لم يُضف بعد.
- **F11:** فتح درس لم يعد يحمّل كل الدروس وكل صفوف التقدم؛ صار يقرأ دروس الوحدة وحدها عبر `list_published_for_unit`، ومجموعة معرفات المكتملة عبر `completed_lesson_ids` (عمود مفهرس واحد). أُضيف اختبار يثبت أن الدرس قيد التقدم لا يفتح successor.
- **P1-9 (جزء):** نشر الإنتاج صار يتحقق من `/ready` (وليس `/health`) ويسجّل SHA السابق ويعمل rollback تلقائياً وإعادة بناء إذا لم تصبح النسخة جاهزة. يبقى بناء الصور على خادم الإنتاج وغياب canary/immutable digests.
- **P2-5/P2-8 (جزء):** أُضيف `deploy/k8s/` (Deployment + PDB + HPA 3–24 + Argo Rollout canary 5/25/100 مع AnalysisTemplate لأخطاء 5xx وp95 + Job ترحيل + README). لا يمكن التحقق منها هنا (لا cluster)، لكنها تستبدل «نشر على خادم واحد وبناء الصورة عليه» بصور digest ثابتة وترحيل منفصل وتراجع آلي. الأسرار عبر `secretRef`/Secret Manager لا `.env`. يبقى WAF/DDoS وNetworkPolicy عند مزوّد الحافة (P1-1/P2-8).
- **F19 (جزء):** استجابة `/admin/stats/overview` صارت مخزّنة 30 ثانية في Redis/الكاش المشترك بمفتاح يشمل النافذة، مع fail-open. اللوحة تتحمّل تأخر نصف دقيقة، لكنها أغلى قارئ في النظام (19 تجميعاً على كل الجداول)؛ الكاش يقلّل معدّل المسح من عدد ضغطات التحديث إلى مرة كل 30 ثانية. يبقى جدول `daily_metrics` + Backfill إذا أثبت القياس أن الـ30 ثانية لا تكفي.
- **F17 (تكملة):** `/ready` صار يخزّن تقريره الثابت لمدة `READY_CACHE_TTL_SECONDS` (2 ثانية افتراضياً) مع `force=True` للمسار القسري؛ كل probe لم يعد يفتح اتصال قاعدة بيانات لسؤال لا يمكن أن يتغير في تلك الثانية. يبقى access-log sampling كما هو.
- **P2-3 (جزء):** `quiz_sessions` صارت تُنظَّف من الجلسات المهجورة: `active` أقدم من `ABANDONED_SESSION_RETENTION_DAYS` (7 أيام افتراضياً) وبلا أي إجابة، عبر `purge_abandoned` بـ`NOT EXISTS` ودفعات 5,000 محدودة بالمعرّف. أي جلسة فيها إجابة واحدة تُحفظ مهما كان عمرها، لأن حذفها يعيد كتابة أرقام النشاط والدقة. الوظيفة اليومية الثالثة في الـworker. يبقى التقسيم الفعلي (partitioning) لـ`review_logs`/`quiz_sessions`/`ai_usage`/`audit_logs`.
- **F24:** جدول `feature_flags` + `FeatureFlagService` (كاش 30s fail-open) يحوّل الإطلاق التدريجي إلى صف: `enabled` + `rollout_percentage` مقيّد 0..100 + `public`. التوزيع دالّي عبر `sha256(key:user_id)`، فالمتعلم يرى نفس الجانب على كل جهاز. واجهتان: `GET /api/v1/flags` معتمدة وتعيد الأعلام العامة مُقيَّمة للمستخدم فقط، و`GET/PUT /api/v1/admin/flags` للإدارة مع صف تدقيق في نفس المعاملة.
- **F21:** مفاتيح الجداول صارت UUIDv7 عبر `uuid7()` في `app/core/ids.py` و`UUIDPrimaryKeyMixin` (و`ai_usage_records`). الطابع الزمني في البتات العليا يجعل الإدراج يلتصق بحافة فهرس الـB-tree بدل صفحات عشوائية، مع عدّاد 12-بت داخل نفس الميلي-ثانية يضمن الترتيب حتى لو رجع ساعة النظام. لا migration: القيمة تُولَّد في التطبيق، والمفاتيح القديمة تبقى صالحة.
- **F25:** كل توكن يحمل `kid` في ترويسته، و`decode_token` يختار المفتاح حسبه بدل تجربة مفتاح واحد: `JWT_KEY_ID` الحالي + `JWT_PREVIOUS_KEY_ID`/`JWT_PREVIOUS_SECRET`/`JWT_PREVIOUS_REFRESH_SECRET` خلال نافذة التدوير. التوكنات القديمة بلا `kid` تُقبل أيضاً، فالتدوير ليس خروجاً إجبارياً. `kid` غير منشور أو توقيع بالمفتاح السابق مع ادعاء `kid` الحالي ⇒ 401.
- **P2-1 (جزء):** أُضيف `DATABASE_READ_URL` و`read_sqlalchemy_dsn`، ومحرك/مصنع جلسات ثانٍ في `app/core/database.py` مع `make_read_uow()` و`ReadUowDep`. الكتالوج العام ولوحة إحصاءات الإدارة يُقرآن من النسخة: الأولى متطابقة لكل متعلم، والثانية أصلًا تتحمل تأخر ثانية؛ التقدم والمصادقة والكتابة تبقى على الأساس. غياب المتغير — أو تطابق الـDSN — يعيد محرك الأساس، فلا pool مزدوج. يبقى قياس `replay_lag` والتحويل التلقائي عند تجاوز 5 ثوانٍ.
- **F10:** `progress.summary` لم يعد يحمّل كل صفوف `lesson_progress`؛ يقرأ `totals_for_user` (sum/counts في استعلام واحد) بدل hydration كامل، بينما `list_for_user` بقي للـunlock map الذي يحتاج الصفوف فعلاً.
- **F15 (جزء منه):** الحذف صار مقسّماً إلى دفعات 5,000 صف مع commit بين الدفعات وحد أقصى 200 دفعة، لجدولين: التوكنات المنتهية، و`ai_usage_records` الأقدم من `AI_USAGE_RETENTION_DAYS` (180 يوماً افتراضياً؛ 0 يعطّل الوظيفة). لم يعد `DELETE` واحداً يمسك الجدول ويكتب WAL ضخماً.

**قاعدة القراءة:** ✅ يعني أن السلوك موجود ومغطّى باختبار في المستودع؛ ⚠️ يعني أن جزءاً منه فقط؛ ❌ يعني أنه قرار بنية تحتية لا يغلقه كود. لا تُرقّى ⚠️ إلى ✅ في تقرير الإنجاز قبل أن يوجد اختبار قبول مقابل.

**اختبارات هذه الدفعة:** `tests/unit/test_health.py` (سياسة الجاهزية)، `tests/unit/test_ai_bulkhead.py` (المهلة والتشبع)، `tests/api/test_metrics.py`، `tests/api/test_media_cache.py`، `tests/unit/test_queue_redis.py` (at-least-once + الاسترجاع)، وإضافات في `tests/unit/test_deps.py`، `tests/unit/test_worker.py`، `tests/integration/test_idempotency.py` (قفل التنفيذ المتزامن) و`tests/integration/test_ai_tutor.py`.

---

### Phase 1 — 20k DAU (4 أسابيع، ~40 يوم-مهندس)
| المهمة | التفاصيل |
|---|---|
| P1-1 Edge | CDN + WAF + LB + قواعد Cache |
| P1-2 PgBouncer | transaction mode + pool مضبوط |
| P1-3 Redis HA | نسختان: cache (`allkeys-lru`) وqueue (`noeviction`) |
| P1-4 Catalogue cache | Redis + version + single-flight + CDN |
| P1-5 Media CDN | S3 + CDN + تحويل الصيغ + `Cache-Control: immutable` (إصلاح F28) |
| P1-6 Streams queue | Consumer Groups + DLQ + Concurrency + Monitor |
| P1-7 Logs/Probes | Sampling + `/ready` cache |
| P1-8 Refresh/Purge | TTL 30m + Batch purge |
| P1-9 Deploy | صور Immutable + 3 نسخ + Rollback |

**معيار الخروج:** 500 RPS لمدة 30 دقيقة، p95 < 250ms، Errors < 0.5%، إسقاط نسخة API بلا أخطاء، Cache hit > 90%.

---

### Phase 2 — 100k DAU (6 أسابيع، ~60 يوم-مهندس)
| المهمة | التفاصيل |
|---|---|
| P2-1 Read replica | Routing صريح للقراءات المتسامحة |
| P2-2 Progress counters | `user_stats` + Backfill + Cache 60s |
| P2-3 Retention/Partition | `review_logs`, `quiz_sessions`, `ai_usage`, `audit_logs` + Purge/Audit للبقية |
| P2-4 AI bulkhead | سقف عالمي + Circuit Breaker + Metering مجمّع |
| P2-5 Autoscaling/Canary | HPA + Canary 5/25/100 |
| P2-6 DR drill | Failover + PITR موثقان |
| P2-7 Chaos | قتل نسخة/Redis/Replica أثناء الحمل |
| P2-8 Security | Secret Manager + WAF + أقل صلاحية |
| P2-9 Consistency | قفل Idempotency أثناء التنفيذ + `ON CONFLICT` للإجابات + منع حذف تاريخ المتعلمين |

**معيار الخروج:** 2,000 RPS لمدة 30 دقيقة، Soak 24 ساعة عند 700 RPS، Pool < 60%، Replica lag < 5s، RTO مُثبت.

---

### Phase 3 — 300k+ (8–12 أسبوعاً، ~70 يوم-مهندس)
| المهمة | شرط التفعيل |
|---|---|
| P3-1 UUIDv7 | Bloat مقاس في جداول Append |
| P3-2 CDC/Warehouse | تقارير تُبطئ القاعدة أو تحتاج تاريخاً |
| P3-3 Sharding/Cells | كتابة الـ Primary > 60% بعد كل التحسينات |
| P3-4 Multi-Region | > 30% مستخدمين خارج المنطقة أو RTO < 15 دقيقة |
| P3-5 Cost | تكلفة/1k طلب تتجاوز الهدف |

---

### الفريق والتقدير
| الدور | التخصيص |
|---|---|
| Tech Lead / Architect | 50% |
| Backend Senior ×2 | 100% |
| SRE/DevOps | 100% |
| QA/Load Engineer | 50% |
| Mobile | 20% (عقد API) |
| Data/Analytics | 20% (Phase 2+) |
| **الإجمالي** | **~190 يوم-مهندس / 6 أشهر** |

---

## 10) خطة اختبار الحمل والتحقق

### 10.1 الأدوات
- **k6** (السيناريو جاهز في `loadtest/k6/natiqo-mixed.js`) + Prometheus Remote Write.
- بيانات: 5,000 حساب اختبار على الأقل — `scripts/mint_loadtest_tokens.py` (حساب لكل VU، وإلا صار حدّ المستخدم هو عنق الزجاجة).
- بيئة Staging بمواصفات قريبة من الإنتاج (لا تختبر على جهاز المطوّر).
- التفاصيل التشغيلية وخطوات التثبيت في `loadtest/README.md`.

```bash
# 1) توكنات: حساب لكل مستخدم افتراضي
uv run python -m scripts.mint_loadtest_tokens \
  --count 5000 --ttl-seconds 28800 --output loadtest/tokens.json

# 2) حمل مختلط ≈ 2,000 طلب/ثانية (1,400 قراءة + 600 كتابة)
#    ملاحظة: learn RPS = عدد الدروس المبدوءة/ثانية، وكل درس ≈ 12 طلباً
k6 run loadtest/k6/natiqo-mixed.js \
  -e BASE_URL=https://staging.example.com \
  -e TOKENS_FILE=$PWD/loadtest/tokens.json \
  -e BROWSE_RPS=400 -e LEARN_RPS=50 -e PROGRESS_RPS=66 \
  -e DURATION=30m
```

### 10.2 السيناريوهات
| السيناريو | التوزيع | السلوك |
|---|---|---|
| `browse` | 60% | `GET /content/tree` + `GET /content/lessons/{id}` + ETag |
| `learn` | 30% | بدء جلسة → 10 إجابات → إكمال |
| `progress` | 10% | `GET /progress` + `/progress/unlocks` + `/review/due` |
| `auth` | نادر | Refresh كل 30 دقيقة |

### 10.3 الاختبارات الإلزامية
1. **Baseline:** 200 RPS، 30 دقيقة.
2. **Load:** 2,000 RPS، 30 دقيقة (Phase 2).
3. **Soak:** 700 RPS، 24 ساعة (تسريب ذاكرة/اتصالات).
4. **Spike:** 0 → 3× في 60 ثانية.
5. **Stress:** زيادة حتى الفشل لتحديد “الركبة”.
6. **Failure injection:**
   - قتل نسخة API.
   - Failover Redis.
   - إيقاف Replica.
   - مزوّد AI يستجيب بعد 20s أو يفشل 100%.
   - قاعدة البيانات: `pg_sleep` في استعلام.

### 10.4 العتبات (Thresholds)
```js
thresholds: {
  http_req_failed: ['rate<0.005'],
  'http_req_duration{kind:read}':  ['p(95)<200', 'p(99)<500'],
  'http_req_duration{kind:write}': ['p(95)<300', 'p(99)<800'],
  checks: ['rate>0.99'],
}
```

### 10.5 ما نقيسه أثناء الاختبار
- RPS، p50/p95/p99، معدل 429/5xx.
- CPU/Memory للـ API، عدد النسخ.
- Pool in-use/waiters، زمن الاستعلامات، Connections، Replica lag.
- Redis ops/latency/evictions، Cache hit.
- Queue lag/DLQ.
- تكلفة تقديرية لكل 1,000 طلب.

### 10.6 معايير النجاح
- Phase 0: 200 RPS، 0 أخطاء 5xx غير متوقعة، p95 < 300ms.
- Phase 1: 500 RPS، p95 < 250ms، Cache hit > 90%، إسقاط نسخة بلا أثر.
- Phase 2: 2,000 RPS، p95 < 300ms، Soak 24h بلا تسريب، Failover < 60s.
- Phase 3: 5,000 RPS، 300k DAU محاكاة، التكلفة ضمن الهدف.

---

## 11) التكاليف التقديرية (شهرياً)

> أرقام استرشادية لمزوّد سحابي مُدار، بأسعار 2026 التقريبية. الغرض هو **رتبة الحجم**، لا الفاتورة الدقيقة.

| البند | المواصفات | التكلفة/شهر |
|---|---|---|
| API | 6 × (4 vCPU / 8GB) | $300–450 |
| Workers | 2 × (4 vCPU / 8GB) | $100–150 |
| PostgreSQL مُدار HA | 8 vCPU / 32GB + 1TB + Replica | $700–1,200 |
| PgBouncer | صغير | $20 |
| Redis HA (cache) | 4GB | $100–150 |
| Redis HA (queue) | 2GB | $60–90 |
| LB + WAF | | $50–120 |
| CDN + Egress | 5–10 TB | $150–400 |
| Object Storage | 2TB | $50 |
| Observability | Metrics/Logs/Traces | $150–350 |
| Backups + PITR | | $80–150 |
| **الإجمالي** | | **~$1,800–3,300** |

**ملاحظات:**
- الاستضافة الذاتية قد تخفض الفاتورة 40–50% لكنها تضيف تكلفة تشغيلية (SRE) ومخاطر أعلى.
- أكبر متغيّر هو Egress الوسائط → CDN إلزامي.
- AI تكلفة منفصلة تماماً؛ ضع لها سقفاً واضحاً ومراقبة يومية.

---

## 12) سجل المخاطر

| المخاطرة | الاحتمال | الأثر | التخفيف |
|---|---|---|---|
| حجب مستخدمين شرعيين بسبب CGNAT | مرتفع | مرتفع جداً | Principal = User + حدود IP عالية + مراقبة 429 |
| استنفاد Pool بسبب AI | مرتفع | مرتفع | فصل الجلسة + Bulkhead + Timeout |
| فقدان وظائف في الطابور | متوسط | مرتفع | Streams + ACK + DLQ + اختبار قتل |
| Migration تقفل جدولاً كبيراً | متوسط | مرتفع | Expand/Contract + CONCURRENTLY + اختبار على نسخة |
| Cache Stampede عند النشر | متوسط | متوسط | Single-Flight + Jitter + SWR |
| Replica lag يعرض تقدماً قديماً | متوسط | متوسط | توجيه صريح + مراقبة + Fallback للـ Primary |
| تكلفة AI تنفجر | متوسط | مرتفع | سقف عالمي + Circuit Breaker + تنبيهات |
| فقدان بيانات | منخفض | كارثي | PITR + اختبار استعادة شهري |
| تسريب أسرار | منخفض | مرتفع | Secret Manager + فحص + تدوير |
| ضغط الفريق | مرتفع | متوسط | أولويات P0/P1 فقط + عدم تنفيذ P3 مبكراً |

---

## 13) قرارات مؤجلة وشروط تفعيلها

| القرار | لماذا الآن؟ | شرط التفعيل |
|---|---|---|
| Microservices | فريق صغير وحدود واضحة أصلاً | > 8 مهندسين أو حاجة Scaling مستقل لوحدة |
| Kafka | Redis Streams تكفي حتى آلاف الأحداث/ثانية | > 5,000 حدث/ثانية أو مستهلكون متعددون |
| Kubernetes | Compose + Load Balancer يكفيان | > 10 عُقد أو حاجة Multi-env معقّدة |
| Sharding | Primary يتحمل حتى 300k DAU بعد التحسين | كتابة Primary > 60% باستمرار بعد Partitioning/Counters |
| Multi-Region Active-Active | التعقيد لا يبرره حجم مستخدمي العراق | > 30% خارج المنطقة أو RTO < 15 دقيقة |
| UUIDv7 للجداول القديمة | الترحيل مكلف | Bloat مقاس > 30% أو إدراج > 5k/ثانية |
| Redis Cluster | HA كافٍ | > 100k ops/s أو > 20GB |

---

## 14) تعريف الإنجاز (Definition of Done)

### للنظام (Phase 2)
- [ ] 2,000 RPS لمدة 30 دقيقة، p95 < 300ms، أخطاء < 0.5%.
- [ ] Soak 24 ساعة بلا تسريب ذاكرة/اتصالات.
- [ ] لا نقطة فشل واحدة في مسار الطلب.
- [ ] PITR مُختبَر: RPO ≤ 5 دقائق، RTO ≤ 60 دقيقة.
- [ ] كل تنبيه له Runbook ومالك.
- [ ] Cache hit > 90% للكتالوج، وCDN > 95% للوسائط.
- [ ] Pool DB < 70% عند الذروة، وReplica lag < 5s.
- [ ] DLQ = 0 في الحالة المستقرة.
- [ ] مراجعة أمنية وسد الفجوات P0.
- [ ] وثائق العقد مع الموبايل محدّثة (README الحالي متأخر عن الكود — راجع §15).

### للـ Pull Request (قائمة مراجعة)
- [ ] لا استعلام جديد بلا فهرس مناسب.
- [ ] لا نداء خارجي داخل معاملة مفتوحة.
- [ ] Endpoint جديد له Metrics + Rate limit + اختبار.
- [ ] أي كتابة جديدة Idempotent.
- [ ] لا `COUNT(*)` على جدول كبير في مسار مستخدم.
- [ ] Migration قابلة للتراجع/متوافقة مع الإصدار السابق.
- [ ] تحديث `docs/` عند تغيير العقد.

---

## 15) ملاحظات جانبية مهمة

1. **README متأخر عن الكود:** يذكر مسارات `/api/v1/content/levels` و`/content/units/...` التي استُبدلت بـ `/content/tree`، ويشير إلى `docs/mobile-integration-ar.md` و`docs/backend-roadmap-ar.md` غير الموجودين. يجب تحديثه لأن الفريق والعميل يعتمدان عليه.
2. **`/metrics` كان مذكوراً في `_QUIET_PATHS` وغير موجود** — أُضيف في Phase 0 (انظر §9.1)؛ لم يبقَ من هذه الملاحظة إلا أن Sentry/OTel ما زالا غير موصولين.
3. **`SENTRY_DSN` و`OTEL_*` إعدادات ميتة** — إما تُنفَّذ أو تُحذف؛ الإعدادات غير المستخدمة خطأ تشغيلي.
4. **`AI_TIMEOUT_SECONDS=20`** أطول من أي SLO واقعي؛ اخفضه.
5. **`database_pool_timeout=30`** أخطر من أي استعلام بطيء؛ اخفضه فوراً.
6. **`GZipMiddleware`** يستهلك CPU على كل استجابة > 400B؛ مع CDN يدعم Brotli، انقل الضغط للحافة.
7. **`ResponseCacheMiddleware`** يقرأ الجسم كاملاً في الذاكرة لكل مسار `@cacheable`؛ مع الكتالوج الصغير لا مشكلة، لكن لا تعمّمه على استجابات كبيرة.
8. **`StaticFiles`** لا يدعم Range بشكل مثالي للصوت؛ CDN يحل ذلك.
9. **الوسائط حالياً `no-store`**: `SecurityHeadersMiddleware` يضع `Cache-Control: no-store` لكل استجابة لا تحمل `@cacheable`، و`StaticFiles` لا يستثني. أي أن كل تشغيل لملف صوت يعيد التحميل. هذا شرحٌ مباشر لبطء محتمل في الدروس الصوتية، ويُصلَح قبل أي CDN (F28).
10. **`Job.run_at` غير منفَّذ**: الحقل موجود في الـ Port ويوحي بجدولة، لكن `to_dict()` لا يمرّره و`RedisQueue` لا يؤجّل شيئاً. أي وظيفة تعتمد عليه ستُنفَّذ فوراً. إما يُنفَّذ (Delayed Set / Streams) أو يُحذف من الـ Port.
11. **أسماء وظائف بلا Handler**: `JobName` يعلن 9 وظائف، والمسجّل منها اثنتان فقط (`CLEANUP_EXPIRED_TOKENS`, `PURGE_AI_USAGE`). أي وظيفة أخرى تُسحب وتُسجَّل كخطأ ثم تُهمَل. عند إضافة الإشعارات/التحليلات، أضف الـ Handler واختباراً يفشل إذا أُعلن اسم بلا معالجة.

---

## 16) الملاحق

### أ. استعلامات تشخيص جاهزة
```sql
-- أبطأ الاستعلامات
SELECT query, calls, mean_exec_time, total_exec_time, rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- فهارس غير مستخدمة
SELECT relname, indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes
WHERE idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC;

-- الاتصالات حسب الحالة
SELECT state, count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC;

-- الجداول الأكبر
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total,
       n_live_tup, n_dead_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;

-- تأخر النسخ
SELECT client_addr, state, replay_lag FROM pg_stat_replication;

-- عمر أطول معاملة
SELECT pid, now() - xact_start AS age, state, query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY age DESC
LIMIT 10;
```

### ب. قائمة تحقق النشر
- [ ] Migration Job نجح.
- [ ] `/ready` يعيد 200 على كل نسخة.
- [ ] Canary 5% لمدة 10 دقائق: Errors < 0.5% وp95 طبيعي.
- [ ] ثم 25% ثم 100%.
- [ ] Rollback جاهز (الصورة السابقة + Feature flags).
- [ ] Dashboards مفتوحة أثناء النشر.

### ج. مصطلحات
| المصطلح | المعنى |
|---|---|
| DAU | المستخدمون النشطون يومياً |
| RPS | طلبات في الثانية |
| p95/p99 | الزمن الذي 95%/99% من الطلبات أسرع منه |
| Pool | مجموعة اتصالات قاعدة البيانات |
| Bulkhead | عزل مورد (مثل AI) حتى لا يستهلك موارد البقية |
| Outbox | جدول أحداث يُكتب في نفس معاملة العمل ثم يُنشر لاحقاً |
| DLQ | قائمة الرسائل الفاشلة نهائياً |
| PITR | استعادة قاعدة البيانات إلى لحظة زمنية |
| RPO/RTO | أقصى فقدان بيانات مقبول / أقصى زمن تعافٍ |
| CGNAT | مشاركة آلاف المستخدمين عنوان IP واحداً |

---

## 17) الخطوات الأولى (أول 14 يوماً) — عملياً

| اليوم | العمل | المخرج |
|---|---|---|
| 1–2 | `/metrics` (منجز) + Sentry + OTel | Dashboards أولية |
| 2–3 | k6 Baseline على Staging | تقرير 200 RPS |
| 3–5 | إصلاح Rate limit (Principal + Token Bucket) | اختبار CGNAT |
| 4–6 | فصل جلسة AI + Bulkhead + Timeout | اختبار 500 طلب AI |
| 5–7 | Migration Job + Advisory Lock | Rolling deploy ناجح |
| 6–8 | Timeouts (DB/uvicorn) + Pool | اختبار 3× حمل |
| 8–10 | PITR + اختبار استعادة | تقرير RTO/RPO |
| 10–12 | Redis منفصل للطابور + استرجاع | اختبار قتل Worker |
| 11–13 | Redis Fail-open + Readiness degraded | قتل Redis 60s → 0 أخطاء 5xx |
| 13–14 | مراجعة النتائج وتحديث الخطة | Go/No-Go للـ Phase 1 |

---

> **الخلاصة النهائية:** Natiqo لا يحتاج إعادة بناء. يحتاج **الانتقال من “تطبيق يعمل” إلى “نظام يُدار”**: قياس، عزل، كاش، طابور موثوق، قاعدة بيانات مضبوطة، ونسخ احتياطي مُختبَر. تنفيذ Phase 0 وPhase 1 وحدهما (6 أسابيع) يرفع السقف من بضع مئات من المستخدمين المتزامنين إلى عشرات الآلاف بأمان، ويجعل Phase 2 (100k DAU) مسألة ضبط سعة لا إعادة تصميم.
