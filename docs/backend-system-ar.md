# نظام الـBackend — الشرح المعماري الكامل

> هذا المستند يشرح **كيف بُني السيرفر** ومن أين يبدأ كل شيء وكيف تترابط الطبقات.
> لِما هو **متاح للعميل الآن** وما هو مخطط، راجع [`mobile-integration-ar.md`](mobile-integration-ar.md)
> و[`backend-roadmap-ar.md`](backend-roadmap-ar.md). وهذان مستندان للعقد والخطة؛ أما هذا المستند
> فهو للبنية الداخلية.

> ⚠️ **لقطة مرحلة (snapshot).** يصف هذا المستند سطح الـAPI في **المرحلة الأولى**
> (حسابات + كتالوج محتوى للقراءة). الـquiz والتقدّم بُنيا لاحقًا، ومسار كتالوج الشرح
> `GET /content/questions/{id}/explanation` **حُذف** — صار الشرح يُعاد مع إجابة الـquiz.
> للحالة الحالية راجع [`mobile-app-architecture.md`](mobile-app-architecture.md)
> و[`client-contract.md`](client-contract.md).

---

## 0. الملخّص في سطرين

**ناتيقو** هو تطبيق تعلّم الإنجليزية (اللهجة البريطانية) للعراقيين. الـBackend مكتوب بـ
**FastAPI + PostgreSQL + Redis + S3**، على شكل **Monolith وحداتي (modular monolith)**
بتقسيم **Clean Architecture داخل كل وحدة**. لا توجد كلمة مرور في النظام إطلاقًا: الدخول
عبر Google أو Apple فقط.

حالة هذه المرحلة: **حسابات + كتالوج محتوى للقراءة**. الـquiz والتقدّم
والمراجعة المتباعدة لم تُبنَ بعد (كل طلباتها تُرجع `404`).

---

## 1. المبادئ المعمارية الخمسة

كل قرار في الكود يتبع واحدًا من هذه المبادئ:

| المبدأ | ماذا يعني عمليًا |
| --- | --- |
| **Monolith وحداتي** | عملية واحدة تُنشر مرة واحدة، لكن كل مجال (users / content / ai) معزول داخل مجلده بعقد واضح. لا اتصال مباشر بين وحدتين. |
| **Ports & Adapters** | الكود يخاطب **مجردات** (`Protocol`) لا تنفيذات. تبديل Redis أو S3 أو مزوّد الـAI موضع واحد: [`app/api/deps.py`](../app/api/deps.py) (جذر التركيب). |
| **Domain نظيف** | طبقة `domain` لا تستورد FastAPI ولا SQLAlchemy ولا Redis. قواعد العمل قابلة للاختبار وحدها. |
| **الوصول لقاعدة البيانات عبر وحدة عمل واحدة** | لا route ولا service يفتح جلسة مباشرة؛ الكل يمرّ عبر `UnitOfWork` + repositories، فالفشل في منتصف عملية يُرجِع كل شيء. |
| **عقد أخطاء ثابت** | كل استجابة خطأ تحمل `code` ثابتًا و`requestId`؛ العميل يتفرّع على الكود لا على النص. |

---

## 2. خريطة المجلدات

```
app/
  main.py          # مصنع التطبيق (application factory) + lifespan
  api/
    deps.py        # ★ جذر التركيب: المكان الوحيد الذي يعرف التنفيذ الملموس لكل منفذ
    v1/
      router.py    # تسجيل مسارات كل وحدة
      routes/      # health.py (liveness/readiness)
  core/            # بنية تحتية مشتركة (لا وحدة تملكه)
    config.py      # الإعدادات من متغيّرات البيئة
    database.py    # محرّك SQLAlchemy الداخلي + مصنع الجلسات
    uow.py         # منفذ وحدة العمل (بلا أي driver)
    uow_sqlalchemy.py  # التنفيذ الملموس
    security.py    # إصدار/تحقّق JWT
    errors.py      # أكواد الأخطاء + هرمية الاستثناءات (بلا framework)
    exceptions.py  # تحويل كل استثناء إلى غلاف الخطأ الموحّد
    middleware.py  # كومة الوسطاء (ASGI)
    http_cache.py  # ETag + Cache-Control اختياريّة لكل route
    idempotency.py # إعادة تشغيل استجابة عند تكرار Idempotency-Key
    pagination.py  # مؤشرات (cursors) غير شفافة لترقيم keyset
    responses.py   # غلاف الاستجابة + الصفحات + WireModel (camelCase)
    events.py      # ناقل أحداث داخلي داخل العملية
    health.py      # فحوص liveness/readiness
    logging.py     # structlog + إخفاء الأسرار
    time.py        # كل شيء UTC + تقويم المستخدم حسب منطقته
    registry.py    # قائمة نماذج الـORM (لاكتشافها كلها)
    base.py/types.py # الأساس المشترك + الأنواع المشتركة
  modules/         # ★ مجالات العمل
    users/  content/  ai/
      domain/         # كيانات + قواعد + منافذ (بلا framework)
      application/    # حالات الاستخدام (use cases) + DTOs
      infrastructure/ # نماذج SQLAlchemy + التنفيذات + mappers
      presentation/   # مسارات FastAPI + مخططات الأسلاك (schemas)
  platform/        # محوّلات خارجية عبر منافذ
    cache/ queue/ storage/ push/ ai/ identity/
```

كل وحدة داخل `modules/` تُقسَّم دائمًا إلى أربع طبقات بالاسم نفسه.

---

## 3. الطبقات الأربع داخل كل وحدة

```
presentation  →  application  →  domain   ←  infrastructure
(HTTP)            (use cases)      (قواعد)      (SQLAlchemy)
```

- **`domain/`** — الكيانات (`Level`, `Unit`, `Lesson`, `Question`, `User` …) والقواعد
  (`policy.py`) والمنافذ (`ports.py`). لا يعرف شيئًا عن HTTP أو قاعدة البيانات.
  مثال: [`app/modules/content/domain/entities.py`](../app/modules/content/domain/entities.py).
- **`application/`** — حالات الاستخدام + كائنات القراءة (view models). تنسّق بين الـdomain
  والمنافذ. مثال: [`catalogue_service.py`](../app/modules/content/application/catalogue_service.py).
- **`infrastructure/`** — التنفيذ الملموس: نماذج SQLAlchemy، الـrepositories، الـmappers
  (تحويل صف ↔ كيان). مثال: [`models.py`](../app/modules/content/infrastructure/models.py).
- **`presentation/`** — المسارات والمخططات (schemas). **رقيقة عمدًا**: تحليل، تفويض، عرض.
  أي تفريع حول «ما يُسمح للمستخدم» مكانه في `application` لا هنا.

الاعتماد يسير باتجاه واحد فقط: `presentation → application → domain`، و`infrastructure`
تُحقّق منافذ الـdomain.

---

## 4. الإقلاع ودورة الحياة

نقطة البداية: `create_app()` في [`app/main.py`](../app/main.py).

### ترتيب الإقلاع (lifespan) — الترتيب مقصود

```python
@asynccontextmanager
async def lifespan(app):
    _check_config(settings)  # 1) ارفض الإقلاع بإعداد غير آمن قبل أي شيء
    configure_logging(...)  # 2) فعّل التسجيل
    import_all_models()  # 3) سجّل كل جداول الـORM
    configure_mappers()  # 4) تحقّق من العلاقات قبل الخدمة (الفشل عند الإقلاع لا عند أول طلب)
    yield
    await drain_ai_usage()  # 5) فرّغ كتابات استخدام الـAI
    await dispose_engine()  # 6) أغلق وصلات قاعدة البيانات
```

- **التحقّق أولًا:** `Settings.validate_environment()` (في [`config.py`](../app/core/config.py))
  يرفض الإقلاع في الإنتاج إذا كان `JWT_SECRET` ما زال قيمة تطوير، أو الأسرار متطابقة، أو
  `REDIS_URL` غير مضبوط، أو لم يُضبط أي مزوّد دخول.
- **تسجيل النماذج:** [`registry.py`](../app/core/registry.py) يسرد كل ملفات النماذج يدويًا،
  لأن نموذجًا غير مُستورَد لا يظهر في `Base.metadata` فيكتب Alembic هجرة **تحذف** الجدول،
  أو يفشل توليد الـDDL بخطأ مفتاح أجنبي.
- **`configure_mappers()`** يتحقق من صحة العلاقات؛ عيّن (relationship) معطوب يفشل عند الإقلاع
  لا عند أول طلب حقيقي.

### ما يُركّب على التطبيق

- `/docs` و`/openapi.json` **مخفيان في الإنتاج** إلا إذا `DOCS_ENABLED` (حتى لا تُنشر واجهة
  تفاعلية على سيرفر عام).
- **الملفات الثابتة** `/media` تُركّب فقط إذا كان المجلد موجودًا (وسائط المنهج: صور/صوت).
- الوسطاء، والـCORS (مع تعريض `ETag`/`Cache-Control`/`Retry-After`)، وضغط GZip بعتبة
  **400 بايت** (الافتراضي 1KiB كان لا يضغط صفحات الكتالوج الصغيرة أصلًا).

---

## 5. كومة الوسطاء (Middleware) — الترتيب الفعلي

الترتيب مهم، ومُتحقَّق منه بالتشغيل (الأخير المُضاف هو **الأخير** في الرحلة إلى الداخل، أي
الأول في العودة):

| # | الوسيط | الدور |
| --- | --- | --- |
| 1 | `IdempotencyMiddleware` | يخزّن استجابة أول عملية ناجحة ويعيدها عند تكرار `Idempotency-Key` |
| 2 | `RequestContextMiddleware` | يولّد `X-Request-ID`، يربطه بالسياق، ويكتب سجل الوصول والمدة |
| 3 | `SecurityHeadersMiddleware` | CSP و`X-Frame-Options` وHSTS… ويضبط `Cache-Control: no-store` افتراضيًا |
| 4 | `BodySizeLimitMiddleware` | يرفض الأجسام الأكبر من الحد (ASGI خام، قبل أي تحليل) |
| 5 | `ResponseCacheMiddleware` | يحسب `ETag` ويردّ `304` للطلبات المُعلَّمة `@cacheable` |
| 6 | `GZipMiddleware` | يضغط الاستجابات ≥ 400 بايت |
| 7 | `CORSMiddleware` | الأصول المسموح بها (لا `*` مع `allow_credentials`) |

قراران لافتان موثّقان في الكود:

- **`ResponseCacheMiddleware` خارج GZip** عمدًا: الـ`ETag` يجب أن يوصف **البايتات المضغوطة
  التي استلمها العميل فعلًا** (RFC 9110 §8.8.3)، وإلا لم يستطع العميل إعادة التحقق → `304`.
- **`no-store` هو الافتراضي:** أي route يريد أن يكون قابلًا للكاش **يُعلن** ذلك عبر
  `@cacheable(...)`؛ ومن ينسى الديكوريتر يبقى `no-store` (الفشل في الاتجاه الآمن).

---

## 6. الأخطاء والغلاف الموحّد

كل خطأ — بما فيه `422` — يعود بالشكل نفسه:

```jsonc
{
  "error": {
    "code": "AUTH_IDENTITY_INVALID",     // تفرّع عليه، لا على message
    "message": "That sign-in could not be verified. Please try again.",
    "details": { },                        // اختياري
    "requestId": "01d7605b12b64d11..."
  }
}
```

- **أكواد ثابتة** في [`app/core/errors.py`](../app/core/errors.py) كـ`StrEnum` (جزء من العقد
  العام). الأكواد محجوزة للمراحل القادمة أيضًا (`QUIZ_*`, `INSUFFICIENT_HEARTS`,
  `REVIEW_NOTHING_DUE`, `AI_QUOTA_EXCEEDED` …) رغم عدم وجود مسارات لها بعد.
- **هرمية `AppError`** تحمل `code` + `http_status` + `details`؛ و`RateLimitedError` و
  `InsufficientHeartsError` تضيف بيانات مفيدة (`retry_after`, `next_heart_in_seconds`).
- **[`exceptions.py`](../app/core/exceptions.py)** يحوّل كل استثناء إلى الغلاف: أخطاء التطبيق،
  أخطاء التحقّق، `IntegrityError` → `409`، `SQLAlchemyError` → `503`، وأي استثناء آخر → `500`
  **بلا أي أثر داخلي** (فقط `request_id`).
- **لا تكرار للأسرار:** رسائل الـdriver لا تُعاد للعميل أبدًا، بل تُسجَّل فقط.

> ملاحظة مهمة في `ERROR_RESPONSES`: مفاتيح الحالات **نصوص** (`"422"`) لا أعداد، وهذا مقصود؛
> لأن FastAPI يفحص المفتاح النصّي، وإن كان عددًا يحقن `HTTPValidationError` الخاص به فينشر
> الـOpenAPI شكلًا لا نرسله أبدًا.

---

## 7. المصادقة (وحدة `users`)

### 7.1 الدخول الاجتماعي — لا كلمات مرور

```
SDK على الجهاز (google_sign_in / sign_in_with_apple)
      → idToken (JWT)
      → POST /api/v1/auth/google | /auth/apple
      → السيرفر يتحقّق من التوقيع + issuer + audience + الصلاحية
      → يبحث عن الهوية بمفتاح (provider, subject)
          موجودة؟  → نفس الحساب دائمًا
          جديدة؟   → يُنشأ الحساب (أول دخول = التسجيل)
```

- **المفتاح هو `(provider, subject)`** حيث `subject` هو المعرّف الثابت لدى المزوّد؛ أما البريد
  فيُستخدم **فقط للربط** بين مزوّدين، وفقط إذا أكّد المزوّد أن البريد موثّق (وإلا فنتيجة
  `AUTH_IDENTITY_CONFLICT` لمنع استيلاء الحساب).
- التنفيذ في [`auth_service.py`](../app/modules/users/application/auth_service.py)، والمنفذ
  `IdentityVerifierPort` في [`identity.py`](../app/modules/users/domain/identity.py).
- **الرموز:** access token مُوقَّع بـ`JWT_SECRET` (15 دقيقة، ذاكرة العميل فقط، غير مخزَّن
  على السيرفر)، وrefresh token مُوقَّع بـ`JWT_REFRESH_SECRET` (30 يومًا) يُخزَّن منه **هاش
  الـ`jti` فقط** ([`security.py`](../app/core/security.py)).
- **التدوير إجباري:** كل refresh يُصدر رمزًا جديدًا؛ وتقديم رمز مُستهلَك = سرقة → **يُبطَل
  كامل سلسلة `family`** (كل الأجهزة). هذا سبب قاعدة single-flight في تطبيق الجوال.
- **التحقق من الدور يقرأ الحساب الحيّ** لا الـclaim داخل التوكن، فسحب صلاحية يسري فورًا.

### 7.2 نقاط النهاية

| الطريقة | المسار | الوصف |
| --- | --- | --- |
| POST | `/auth/google` | دخول/إنشاء عبر Google ID token |
| POST | `/auth/apple` | دخول/إنشاء عبر Apple ID token |
| POST | `/auth/refresh` | تدوير رمز التحديث → زوج جديد |
| POST | `/auth/logout` | إبطال الجلسة الحالية أو كل الأجهزة |
| GET / PATCH / DELETE | `/me` | قراءة/تعديل الملف والتفضيلات/إغلاق الحساب |
| GET | `/me/devices` | الأجهزة المسجَّلة |
| DELETE | `/me/devices/{id}` | إنهاء جلسة جهاز |

- `DELETE /me` = **إغلاق فوري لا رجعة فيه** (soft delete يحفظ الصف للسجل)، ويُبطل كل سلاسل
  الـrefresh في نفس المعاملة.
- **`dev-login`** (`admin/admin`) باب خلفي ثابت يُركَّب متى كان `DEV_LOGIN_ENABLED` مفعّلًا،
  وهو **معطّل افتراضيًا**، و**الإنتاج يرفض الإقلاع** ما دام مفعّلًا (`validate_environment`)
  لأن بياناته ثابتة في المستودع لا أسرار نشر. عند إطفائه يغيب المسار كليًا من كائن التطبيق
  (لا في الراوتر ولا في OpenAPI). اعتماد `admin`/`admin` في الإنتاج يعني أن أي شخص يعرفها
  قادر على الدخول.

---

## 8. وحدة المحتوى (الكتالوج)

### 8.1 الشجرة والأحوال التحريرية

```
Level (A1..C2) → Unit → Lesson → Question
```

- **الظهور يورَث للأسفل:** السؤال مرئي بالضبط عندما يكون درسه منشورًا (لا `state` للسؤال).
- **دورة الحالة:** `draft → review → approved → published → archived` مع انتقالات محكومة
  ([`types.py`](../app/core/types.py))، ولا يُنشر ابن قبل أبيه.
- **أنواع الأسئلة:** `multiple_choice`, `multi_select`, `true_false`, `fill_blank`,
  `word_bank`, `ordering`, `matching` (تُضاف أنواع جديدة فقط بُعد أن يقدّر المحرّك تصحيحها).

### 8.2 مفتاح الإجابة محجوب عمدًا

- الكيان `Question` يحمل `correct_index` و`accepted_answers` و`explanation` (يحتاجها محرّك
  المحاولات لاحقًا)، لكن **طبقة العرض لا تُصدرها أبدًا**.
- طبقتان مستقلّتان للحماية: (أ) كائن القراءة `QuestionView` **لا يذكر** الحقول، (ب) مخطط
  الأسلاك `QuestionResponse` **لا يعلنها** ([`schemas.py`](../app/modules/content/presentation/schemas.py)).
  إضافة حقل للكيان لا تكفي لنشره.
- الشيء الوحيد الذي يعيد الإجابة (الصحّ + الشرح) هو محرّك المحاولات:
  `POST /quiz/sessions/{id}` بأمر `{"op":"answer"}`. (سابقًا كان هناك مسار كتالوج منفصل
  `GET /content/questions/{id}/explanation` وقد **حُذف** فلم يبقَ للكتالوج سطح يكشفها.)
- **غير المنشور وغير الموجود = نفس `404`**؛ لا `403` (لئلا يصبح الـAPI شريط تقدّم لخطّة التحرير).

### 8.3 نقاط النهاية (عامة بلا مصادقة)

| المسار | الوصف |
| --- | --- |
| `GET /content/tree` | الشجرة كاملة في ردّ واحد: المستويات ← الوحدات ← الدروس، مع العدّادات |
| `GET /content/lessons/{id}` | درس + أسئلته **بلا مفتاح إجابة** |

- **الشجرة كاملة بلا ترقيم:** مسار واحد غير مصفّح يعيد الهيكل كله (nest) بعدّادات
  `unitCount`/`lessonCount`/`questionCount`، فلا يوجد مؤشّر يتنقّل العميل عبره. الدروس تُترك
  للتفاصيل عبر `/content/lessons/{id}`. (سابقًا كان ثلاثة مسارات مصفّحة
  `/content/levels`، `/content/levels/{code}/units`، `/content/units/{id}/lessons` **وقد حُذفت**.)
- **العدّ باستعلام مجمَّع واحد** لكل شجرة، لا استعلام لكل صف.
- **قابل للكاش:** `ETag` + `Cache-Control: max-age=60, stale-while-revalidate=600`، وإعادة
  التحقق بـ`If-None-Match` تردّ `304` بلا جسم. `404` لا تُوضع في الكاش.

---

## 9. وحدة العمل (Unit of Work) والمستودعات

- المنفذ [`uow.py`](../app/core/uow.py) **لا يستورد أي driver**، فطبقات الـapplication تعتمد
  عليه دون سحب SQLAlchemy إلى import graph.
- التنفيذ [`uow_sqlalchemy.py`](../app/core/uow_sqlalchemy.py) يفتح معاملة واحدة، ويعرّض
  repositories لكل جدول (`uow.users`, `uow.levels`, `uow.questions` …).
- **قاعدة:** الخروج من `async with uow:` دون `commit()` = `rollback`. فلا تُحفظ أنصاف
  العمليات، وهذا ما يجعل «إنهاء درس» الذي يمسّ التقدّم + النقاط + السلسلة + الإنجازات ذرّيًا.
- الـrepositories **لا تُنفّذ `commit` بنفسها أبدًا**؛ وحدة العمل تملك المعاملة.
- المستودعات تُفلتر على «المنشور» **داخل الاستعلام**، فالاسم `get_published_*` تعهّد لا خيار:
  [`repositories.py`](../app/modules/content/infrastructure/repositories.py).

---

## 10. منصة المزوّدات (Ports & Adapters)

كل خدمة خارجية وراء منفذ في `app/platform/*/port.py`، والتنفيذ يُختار من الإعدادات داخل
جذر التركيب [`deps.py`](../app/api/deps.py):

| المنفذ | التطوير والاختبار | الإنتاج |
| --- | --- | --- |
| `CachePort` | `InMemoryCache` | `RedisCache` (عند ضبط `REDIS_URL`) |
| `QueuePort` | `InMemoryQueue` | `RedisQueue` |
| `ObjectStoragePort` | `LocalObjectStorage` | `S3ObjectStorage` |
| `PushPort` | `LoggingPush` | `FcmPush` (اختياري؛ فشله لا يمنع الإقلاع) |
| `AIProvider` | `DisabledProvider` | `OpenAIProvider` / `AnthropicProvider` |
| `IdentityVerifierPort` | — | Google/Apple عبر مفاتيح JWKS المخزَّنة مؤقتًا |

تجربة **صفر خدمات خارجية**: ضع `DATABASE_URL=sqlite+aiosqlite:///./local.db` و`REDIS_URL=`
فيُستخدم الكاش/الطابور داخل العملية. المحوّلات تُخزَّن بالعملية عبر `lru_cache` (تجمع واحد
لا واحد لكل طلب)، و`reset_composition_root()` يُفرغها للاختبارات.

**الـAI تحسين لا اعتماد:** كل مسار AI له نتيجة بديلة حتميّة، وتُسجَّل تكلفة كل نداء في
`ai_usage_records`، والميزانية اليومية قاعدة في الـdomain ([`budget.py`](../app/modules/ai/domain/budget.py))
بحسب اليوم التقويمي في منطقة المستخدم.

---

## 11. قاعدة البيانات والهجرات

- **قاعدة مشتركة** ([`base.py`](../app/core/base.py)): مفتاح UUID v4 (غير قابل للتعداد)،
  و`created_at`/`updated_at` بمنطقة زمنية UTC، وأسماء قيود **حتمية** (ليصحّ توليد Alembic).
- الجداول الحالية (11): `users`, `user_identities`, `user_profiles`, `user_settings`,
  `refresh_tokens`, `user_devices`, `ai_usage_records`, `levels`, `units`, `lessons`,
  `questions`.
- **الفهارس تُطابق استعلامات الـkeyset الدقيقة** (`parent, state, position, id`) ليبقى ترقيم
  الكتالوج رخيصًا مع نمو المنهج.
- **حرّاس على مستوى قاعدة البيانات** يدعمون قواعد الـdomain: `CHECK (position >= 1)`،
  `UNIQUE (parent_id, position)`، وفهارس `correct_index` غير سالب…
- **الهجرات** في [`alembic/versions/`](../alembic/versions) (تُشغَّل بـ`alembic upgrade head`)،
  وهي **خاصة بـPostgreSQL**؛ للاختبارات/التشغيل المحلي يُبنى المخطط بـ`create_all()`.

---

## 12. الأمان والتصلّب

- **لا أسرار في الكود:** كل القيم الحسّاسة عبر `Settings` (من البيئة/`.env`)، و`validate_environment`
  يرفض قيم التطوير في الإنتاج.
- **إخفاء السجلات:** [`logging.py`](../app/core/logging.py) يستبدل أي مفتاح يشبه السرّ
  (`token`, `authorization`, `api_key`, `jwt_secret`, `dsn` …) بـ`***redacted***`.
- **تقييد الجسم** قبل التحليل بحماية من استنزاف الذاكرة، و**تقييد المعدّل** لكل مسار (نطاقات
  منفصلة: `auth` / `catalogue` / `default`).
- **تصلّب الرؤوس:** CSP (`default-src 'none'` للـAPI، سياسة خاصة لصفحة الـdocs)،
  `X-Frame-Options: DENY`، `Referrer-Policy`، وHSTS في الإنتاج.
- **`X-Forwarded-For` لا يُصدَّق** إلا خلف بروكسي تتحكم به (`TRUST_FORWARDED_FOR`)، لئلا
  يتلاعب عميل بميزانيته.
- **عدم التكرار (Idempotency):** المفتاح مقيّد بـ(الطريقة + المسار + المستخدم)؛ تُخزَّن
  استجابات `2xx` فقط، وإعادة المفتاح بجسم مختلف = `409`.

---

## 13. الأداء

- **غلاف camelCase موحّد** ([`responses.py`](../app/core/responses.py)) عبر `WireModel`، فيقلد
  عقد عميل Flutter بلا `Field(alias=...)` مكتوب يدويًا.
- **ترقيم keyset + جلب `limit + 1`** لتقرير وجود صفحة تالية دون `COUNT`.
- **`ETag`/`304`** و**GZip** يقلّلان بايتات الجوال المتكررة.
- **استعلام تجميعي واحد** لأعداد الوحدات/الدروس في صفحة، لا استعلام لكل صف.
- `lazy="raise_on_sql"` على العلاقات: أي وصول كسول غير مقصود **يفشل بصوت عالٍ** بدل إطلاق
  `SELECT` صامت لكل صف.
- التسجيل/الوقت: كل شيء UTC للتخزين، والتقويم (السلسلة/الهدف اليومي) بمنطقة المستخدم
  ([`time.py`](../app/core/time.py)).

---

## 14. الاختبارات وبوابات الجودة

```
tests/unit/        # الإعدادات، الـpagination، الـhttp_cache، الـdomain، المحوّلات المحلية، التسجيل
tests/integration/ # الكتالوج، المستخدمون/المصادقة، الهجرات، البذور، الـidempotency
tests/api/         # نقاط النهاية عبر ASGI + dev-login
```

- أدوات: `pytest` + `pytest-asyncio` + `aiosqlite`، و`ruff` + `pyright` للجودة.
- الاختبارات تعتمد `reset_composition_root()` و`reset_settings_cache()` و`reset_event_bus()`
  لعزل الحالة بين الاختبارات.
- اختبارات حاسمة موثّقة في الكود: منع تسريب مفتاح الإجابة (`hasattr`)، ثبات غلاف الخطأ في
  `openapi.json`، وصحة ترقيم المؤشّرات.

---

## 15. التشغيل والنشر

**محليًا:**

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head        # PostgreSQL
uv run python -m scripts.seed      # حسابات تجريبية
uv run uvicorn app.main:app --reload --port 8100
```

**إنتاجيًا:** [`docker-compose.yml`](../docker-compose.yml) يشغّل `db` و`redis` و`app`:
- قاعدة البيانات وRedis **داخليتان فقط** (لا ينشران منفذًا)، والسطح الوحيد المكشوف هو الـAPI
  على `:8000`.
- فحوص صحّة (`healthcheck`) تضمن ترتيب الإقلاع، ومجلد وسائط كـvolume مسمّى يعمل بمستخدم غير
  جذري.

**الفحوص:** `/health` (liveness، لا يلمس أي تبعية) و`/ready` (readiness، يفحص قاعدة البيانات
والكاش؛ يعود `503` إذا تعذّر أحدهما).

---

## 16. النقاط الحيّة اليوم (18 عملية)

```
POST   /api/v1/auth/google          POST   /api/v1/auth/apple
POST   /api/v1/auth/refresh         POST   /api/v1/auth/logout
GET    /api/v1/me                   PATCH  /api/v1/me
DELETE /api/v1/me                   GET    /api/v1/me/devices
DELETE /api/v1/me/devices/{id}
GET    /api/v1/content/tree         GET    /api/v1/content/lessons/{id}
POST   /api/v1/quiz/sessions        GET    /api/v1/quiz/sessions/{id}
POST   /api/v1/quiz/sessions/{id}
GET    /api/v1/progress             GET    /api/v1/progress/unlocks
GET    /health                      GET    /ready
```

> الـquiz والتقدّم يظهران هنا لأنهما مبنيّان فعلًا: `POST /quiz/sessions/{id}` مسار **موحَّد**
> لأوامر الإجابة/الإنهاء/الإلغاء عبر حقل `op` (discriminated union)، و`GET /progress` يجمع
> النظرة العامّة مع أوّل صفحة من سجل الـXP، و`GET /progress/unlocks` خريطة الفتح.
>
> **`POST /auth/dev-login` مستثنى من هذه القائمة عن قصد**: يُركَّب متى كان `DEV_LOGIN_ENABLED`
> مفعّلًا (وهو معطّل افتراضيًا، والإنتاج يرفض الإقلاع معه)، فمساره اختياري ولا يُحتسب ضمن الـ18.
> فعّله أو عطّله بالمفتاح (انظر §7).

---

## 17. المبني مقابل المُخطَّط

| مبني اليوم | مُخطَّط (لا مسارات له بعد → `404`) |
| --- | --- |
| الحسابات والدخول الاجتماعي وإدارة الأجهزة | مراجعة متباعدة / SRS (المرحلة 2) |
| كتالوج المحتوى (قراءة فقط: الشجرة + تفاصيل الدرس) | قلوب/سلسلة/إنجازات متقدّمة (المرحلة 3) |
| محرّك المحاولات (quiz): بدء/استئناف/إجابة/إنهاء/إلغاء | اشتراكات ودفع (المرحلة 4) |
| التقدّم وXP (النظرة العامّة + خريطة الفتح) | إشعارات دفع، وسائط مستخدم، AI، مفردات (5–9) |
| ~~شرح السؤال بعد الإجابة~~ (حُذف مسار الكتالوج؛ الشرح الآن مع إجابة الـquiz) | |
| مقياس استخدام الـAI والخلفيات البديلة | |
| الكاش وETag والتقييد وعدم التكرار | |

أكواد الأخطاء والثوابت والأحداث (`QuizCompleted`, `QuestionAnswered` …) **محجوزة مسبقًا**
لكنها ليست موصولة بأي مسار أو تخزين حتى الآن.

---

## 18. قواعد للمساهم الجديد

1. **لا تضع منطق HTTP في الـdomain**، ولا منطق عمل في الـroute.
2. **لا تفتح جلسة قاعدة بيانات خارج `UnitOfWork`**، ولا تنفّذ `commit` داخل repository.
3. **أي تبعية خارجية جديدة = منفذ + محوّل + سطر في `deps.py`**، لا استيراد مباشر في الـapplication.
4. **أي مسار جديد أضف له `code` في `errors.py`** وضعه في الغلاف الموحّد.
5. **قابلية الكاش تُعلن صراحةً** بـ`@cacheable`، والافتراضي `no-store`.
6. **الزمن UTC للتخزين، وتقويم المستخدم للحسابات الزمنية.**
7. **لا تكشف مفتاح الإجابة في أي حمولة عامة**، ولا تُفرّق بين «غير موجود» و«غير منشور».
8. **أضف النموذج إلى `registry.py`** عند إنشاء جدول جديد، والشكل إلى `ERROR_RESPONSES` تلقائي.
