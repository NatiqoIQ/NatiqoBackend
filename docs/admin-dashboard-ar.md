# لوحة تحكم Natiqo — مواصفة كاملة للتنفيذ

للصورة الكاملة — كل شاشة تحتاجها اللوحة، وكل endpoint يجب بناؤه، والمراحل — انظر
[dashboard-master-plan-ar.md](dashboard-master-plan-ar.md). هذا الملف هو المرجع التنفيذي لما
**موجود اليوم**؛ وذاك هو خطة المنتج والمراحل.

مرجع التنفيذ الوحيد هو هذا الملف. كل مسار وكل حقل مذكور هنا مأخوذ من كود الخادم الحالي، لا من
تصوّر مسبق. المصدر الرسمي القابل للتغيير هو `GET /openapi.json` (متاح خارج الإنتاج، أو عند
`DOCS_ENABLED=true`)، وهذا الملف يغطي ما لا يصفه الـ schema: الصلاحيات، دورة حياة المحتوى،
قواعد الصحة لكل نوع سؤال، والفجوات التي يجب سدّها في الـ API قبل بناء شاشات معيّنة.

---

## 1. النطاق والقرارات المعمارية

**ما تبنيه:** تطبيق ويب منفصل (SPA) يتحدث مباشرة مع الـ API الحالي. لا حاجة إلى BFF ولا إلى
خادم وسيط: كل ما تحتاجه اللوحة موجود تحت `/api/v1`.

| القرار | الاختيار | السبب |
| --- | --- | --- |
| نمط التطبيق | SPA (React/Vue/Svelte) + عميل HTTP واحد | الـ API عديم الحالة، والمصادقة بـ Bearer، ولا توجد جلسات على الخادم |
| الأصل (origin) | أصل واحد مسجَّل في `CORS_ORIGINS` | `*` مرفوض عند `CORS_ALLOW_CREDENTIALS=true`، والخادم يرفض الإقلاع بهذا التركيب |
| تخزين الرموز | `sessionStorage` أو متغيّر في الذاكرة + تحديث صامت | لا حاجة لـ `localStorage` دائم للوحة داخلية؛ الجلسة تُبنى من جديد عند إعادة التحميل |
| العنوان الأساسي | متغيّر بيئة `VITE_API_BASE_URL` (أو ما يعادله) | الإنتاج: `http://192.168.69.67:8000` · التطوير: `http://127.0.0.1:8100` |
| بادئة الـ API | `/api/v1` | المسارات أدناه مكتوبة نسبةً إلى هذا البادئة ما لم يُذكر خلاف ذلك |
| اللغة | عربية RTL افتراضيًا، مع الإنجليزية كلغة ثانية للواجهة | المحتوى نفسه بريطاني، لكن جمهور اللوحة فريق عراقي |

**خارج النطاق في هذه المرحلة:** تعديل ملف المتعلّم (`PATCH /me`) من داخل اللوحة، وإدارة
الاشتراكات والمدفوعات (لا يوجد لها مسار بعد)، ولوحة تحليلات حقيقية (انظر القسم 10).

---

## 2. التهيئة قبل أول سطر واجهة

### 2.1 على الخادم

```dotenv
# .env على الخادم
CORS_ORIGINS=http://192.168.69.67:3000,http://localhost:3000
CORS_ALLOW_CREDENTIALS=true
DEV_LOGIN_ENABLED=true          # على الحالة الافتراضية؛ يوفّر دخول admin/admin
```

`CORS_ORIGINS` قائمة أصول مفصولة بفواصل (أو مصفوفة JSON). الخادم يتحقّق منها عند الإقلاع
ويوقف العملية إذا اجتمعت `*` مع السماح بالاعتمادات. بعد التعديل يجب إعادة تشغيل الحاوية.

خارج الإنتاج، وما دام `DEV_LOGIN_ENABLED` مفعّلًا (وهو الافتراضي)، يُمنح تلقائيًا كل أصل
loopback على أي منفذ (`http://localhost:5173`، `http://127.0.0.1:8100`، `http://[::1]:3000`).
هذا ما يجعل `POST /api/v1/auth/dev-login` قابلًا للنداء من خادم تطوير اللوحة دون تسجيل منفذه،
لأن Vite ينتقل إلى المنفذ التالي إذا كان السابق مشغولًا. والمنحة تختفي في الإنتاج وعند إطفاء
العلم. أما أصل الشبكة المحلية مثل `http://192.168.69.67:3000` فليس loopback ويجب ذكره نصًا في
`CORS_ORIGINS`. وللأصول التي لا يمكن حصرها (نشر معاينة لكل فرع) يوجد `CORS_ALLOW_ORIGIN_REGEX`
يقبل تعبيرًا نمطيًا يُطابَق مع ترويسة `Origin` كاملة.

**عَرَض الخطأ:** إذا لم يُمنح الأصل، لا يعيد المتصفح رمز حالة ولا يظهر الطلب في سجل الخادم؛
يبدو `dev-login` معطلًا بينما الخادم سليم. فحص سريع من الطرفية:

```bash
curl -i -X OPTIONS http://127.0.0.1:8100/api/v1/auth/dev-login \
  -H "Origin: http://localhost:5173" -H "Access-Control-Request-Method: POST"
```

ظهور `access-control-allow-origin` يعني أن المنحة سليمة؛ غيابه يعني أن الأصل خارج القائمة.

### 2.2 على اللوحة

```dotenv
VITE_API_BASE_URL=http://192.168.69.67:8000
```

### 2.3 الدخول التجريبي

المسار الوحيد الذي يقبل اسم مستخدم وكلمة مرور هو `POST /api/v1/auth/dev-login`. الحساب
الافتراضي `admin` / `admin` ويُمنح دوري `admin` و`support` عند أول دخول، لذلك يملك كل
الصلاحيات. **في الإنتاج** يجب تغيير `DEV_ADMIN_PASSWORD` أو إيقاف المسار بـ
`DEV_LOGIN_ENABLED=false` بعد ربط دخول Google، لأن الرموز الصادرة منه تعيش 10 سنوات
(`DEV_LOGIN_TOKEN_TTL_SECONDS`).

تسجيل الدخول عبر Google/Apple متاح تقنيًا، لكنه يشترط أن يكون معرّف عميل الويب ضمن
`GOOGLE_CLIENT_IDS`، وهو ما لا يحدث تلقائيًا. لا تبنِ شاشة الدخول على Google قبل التحقّق من
ذلك، وإلا ستحصل على `401 AUTH_PROVIDER_UNAVAILABLE`.

---

## 3. المصادقة والصلاحيات

### 3.1 التدفق

```http
POST /api/v1/auth/dev-login
Content-Type: application/json

{ "username": "admin", "password": "admin",
  "device": { "platform": "web", "appVersion": "1.0.0", "userAgent": "dashboard" } }
```

الاستجابة (أسماء الحقول camelCase دائمًا):

```json
{
  "user": {
    "id": "…", "email": "admin@natiqo.dev", "status": "active",
    "roles": ["admin", "support"], "emailVerified": true,
    "createdAt": "2026-10-18T09:00:00Z",
    "profile": { "displayName": "Dev Admin", "avatarKey": null,
                 "nativeLanguage": "ar", "englishVariant": "british",
                 "timezone": "Asia/Baghdad", "currentLevel": "A1" },
    "settings": { "locale": "ar-IQ", "explanationLanguage": "ar",
                  "notificationPreferences": { }, "dailyGoalMinutes": 10,
                  "dailyGoalNewWords": 5 }
  },
  "tokens": {
    "accessToken": "…", "refreshToken": "…", "tokenType": "Bearer",
    "expiresIn": 315360000, "refreshExpiresIn": 315360000
  }
}
```

* كل طلب محمي: `Authorization: Bearer <accessToken>`.
* عند `401 AUTH_TOKEN_EXPIRED` → `POST /auth/refresh` بالجسم
  `{ "refreshToken": "…", "device": { … } }` ثم أعد الطلب الأصلي **مرة واحدة**.
* عند `AUTH_TOKEN_INVALID` أو `AUTH_TOKEN_REVOKED` أو `AUTH_REFRESH_REUSED` → امسح الجلسة
  وارجع لشاشة الدخول بدون إعادة محاولة.
* الخروج: `POST /auth/logout` بالجسم `{ "refreshToken": "…" }` (أو `allDevices: true`).
  العملية idempotent، فتكرارها لا يفشل.
* شاشة «الحساب»: `GET /me` لعرض الاسم والأدوار، و`GET /me/devices` لعرض الجلسات.

### 3.2 مصفوفة الأدوار

الأدوار مرتّبة، والشرط دائمًا «الحد الأدنى»: صاحب دور أعلى يمرّ من بوابة أدنى.

| الدور | الرتبة | ما يستطيعه في اللوحة |
| --- | --- | --- |
| `user` | 0 | لا شيء |
| `support` | 1 | لا شيء من مسارات `/admin` — يحتاج endpoints قراءة جديدة |
| `content_editor` | 2 | إنشاء/تعديل/حذف المنهج والأسئلة، ونقل الحالة حتى `approved` |
| `content_reviewer` | 3 | كل ما سبق + `POST /admin/content/lessons/{id}/publish` |
| `admin` | 4 | كل ما سبق + إدارة المستخدمين والأدوار |

التحقّق يقرأ الحساب الحيّ لا الادعاء داخل الرمز، فسحب دور يسري على الطلب التالي مباشرة.

**قاعدة واجهة ملزمة:** أخفِ عناصر التحكم التي لا يملك المستخدم صلاحيتها، لكن لا تعتمد على
الإخفاء كحماية — كل مسار محمي على الخادم على أي حال.

**تحذير صلاحيات معروف:** `PATCH /admin/content/lessons/{id}` بجسم `{"state":"published"}`
متاح لدور `content_editor` (رتبة 2) وينفّذ النشر فعليًا، بينما `POST .../publish` يشترط
`content_reviewer`. حتى يُغلق هذا على الخادم، **لا تعرض خيار `published` في قائمة الحالة
لدور المحرّر**، واعتبر مسار `publish` هو زر النشر الوحيد في اللوحة.

### 3.3 تسجيل الخروج الإجباري عند تغيير الأدوار

`POST /admin/users` يستبدل أدوار الحساب كاملةً. إن كان الحساب هو حسابك أنت، أعد تحميل `/me`
مباشرة، لأن الواجهة قد تعرض أزرارًا لم تعد مسموحة.

---

## 4. عقد الاتصال العام

### 4.1 التسمية

* **الاستجابات camelCase دائمًا** (`nextCursor`, `estimatedMinutes`, `imageUrl`).
* **الطلبات تقبل الشكلين** (`displayName` و`display_name`). اكتب camelCase.
* الأجسام ترفض الحقول المجهولة (`extra="forbid"`)، فالحقل الزائد `422` لا يُتجاهل.

### 4.2 غلاف الأخطاء

```json
{ "error": { "code": "VALIDATION_ERROR",
             "message": "…", "details": { "problems": ["…"] },
             "requestId": "fb11336e…" } }
```

| الحالة | المعنى في اللوحة |
| --- | --- |
| `401` | انتهت الجلسة أو الرمز غير صالح → تحديث أو خروج |
| `403` | الدور لا يكفي (`AUTH_INSUFFICIENT_ROLE`) → رسالة «لا تملك صلاحية» |
| `404` | العنصر غير موجود أو محذوف → أغلق النموذج وحدّث القائمة |
| `409` | تعارض حالة (`CONFLICT`, `INVALID_STATE_TRANSITION`) → أعد تحميل العنصر قبل إعادة المحاولة |
| `422` | جسم غير صالح. اقرأ `details.problems` واربطها بالحقول |
| `429` | تجاوز الحد → انتظر `Retry-After` ثانية ثم أعد المحاولة |
| `413` | الجسم أكبر من 1 ميجابايت (لا يحدث إلا في الرفع المباشر) |

اعرض `requestId` في نافذة الخطأ؛ هو ما يربط الشكوى بسجل الخادم.

### 4.3 الصفحات

كل القوائم مرقّمة بمؤشر (cursor) لا بإزاحة:

```json
{ "items": [ … ], "meta": { "limit": 20, "hasMore": true, "nextCursor": "eyJ2Ijox…" } }
```

* `limit` افتراضي 20، وأقصى 100.
* مرّر `nextCursor` كما هو في `?cursor=` ولا تحلّله.
* استثناء واحد: `GET /admin/users` يعيد `{ items, total, hasMore, nextCursor }` بدون `meta`
  (الغلاف القديم)، لأن الشاشة تحتاج العدد الكلي.

### 4.4 حدود المعدّل

| النطاق | الحد الافتراضي |
| --- | --- |
| `/auth/*` | 10 / دقيقة |
| `/admin/*` | 120 / دقيقة |
| `/media/*` | 120 / دقيقة |

الحد لكل IP. جدول المحتوى الذي يجلب الصفحات بسرعة قد يستهلكه؛ اجعل الجلب محدودًا ومخزّنًا
مؤقتًا (SWR أو ما يعادله) بدل الجلب عند كل ضغطة مفتاح.

### 4.5 تفاصيل عملية أخرى

* **لا تستخدم شرطة مائلة أخيرة**: `GET /admin/content/levels/` يعيد `307`، وبعض العملاء
  يفقدون الاستعلام عند التتابع.
* **gzip** يُطبّق تلقائيًا فوق 400 بايت مع `Accept-Encoding: gzip`.
* **`Idempotency-Key`** مقبول على الطلبات المحوِّلة، ومفيد لزر «إنشاء» عند الشبكات الضعيفة.
  إعادة المفتاح بنفس الجسم تعيد الاستجابة الأصلية (24 ساعة).
* **الكاش**: مسارات `/admin` ليست cacheable. لا تعتمد على `ETag` فيها. `ETag` موجود فقط على
  `/content/tree` و`/content/lessons/{id}` العامّين، وهما للقراءة فقط.

---

## 5. مرجع الـ API الكامل للوحة

كل المسارات أدناه نسبية إلى `/api/v1`. عمود «الدور» هو الحد الأدنى.

### 5.1 المصادقة

| الطريقة | المسار | الدور | الجسم | الاستجابة |
| --- | --- | --- | --- | --- |
| POST | `/auth/dev-login` | عام | `username`, `password`, `device?` | `AuthResponse` |
| POST | `/auth/google` | عام | `idToken`, `displayName?`, `device?` | `AuthResponse` |
| POST | `/auth/apple` | عام | `idToken`, `displayName?`, `device?` | `AuthResponse` |
| POST | `/auth/refresh` | عام | `refreshToken`, `device?` | `AuthResponse` |
| POST | `/auth/logout` | عام | `refreshToken`, `allDevices?` | `{ "detail": "Signed out." }` |
| GET | `/me` | مستخدم | — | `MeResponse` |
| GET | `/me/devices` | مستخدم | — | `Device[]` |
| DELETE | `/me/devices/{deviceId}` | مستخدم | — | `204` |

### 5.2 المنهج — المستويات

| الطريقة | المسار | الدور | ملاحظات |
| --- | --- | --- | --- |
| GET | `/admin/content/levels` | `content_editor` | كل الحالات، مرتّبة بالموضع، مع الصفحات |
| POST | `/admin/content/levels` | `content_editor` | ينشئ المستوى في `draft` |
| GET | `/admin/content/levels/{levelId}` | `content_editor` | |
| PATCH | `/admin/content/levels/{levelId}` | `content_editor` | جزئي: الحقول الموجودة فقط تُعدّل |
| DELETE | `/admin/content/levels/{levelId}` | `content_editor` | يحذف المستوى ووحداته ودروسه تتابعيًا |

جسم الإنشاء:

```json
{ "code": "A1", "title": "المستوى المبتدئ", "description": "…", "position": 1 }
```

`code` من `A1, A2, B1, B2, C1, C2`. `title` 1..200. `position` ≥ 1 وفريد بين المستويات
(وإلا `409`). جسم التعديل يضيف `state`.

### 5.3 الوحدات والدروس

| الطريقة | المسار | الدور |
| --- | --- | --- |
| GET / POST | `/admin/content/levels/{levelId}/units` | `content_editor` |
| GET / PATCH / DELETE | `/admin/content/units/{unitId}` | `content_editor` |
| GET / POST | `/admin/content/units/{unitId}/lessons` | `content_editor` |
| GET | `/admin/content/lessons/{lessonId}` | `content_editor` |
| PATCH / DELETE | `/admin/content/lessons/{lessonId}` | `content_editor` |
| POST | `/admin/content/lessons/{lessonId}/publish` | `content_reviewer` |

* جسم إنشاء الوحدة: `{ "title", "description?", "position?" }`. `position` فريد داخل المستوى.
* جسم إنشاء الدرس: `{ "title", "summary", "estimatedMinutes", "position?" }`
  حيث `summary` 1..1000 و`estimatedMinutes` 1..240 و`position` فريد داخل الوحدة.
* `GET /admin/content/lessons/{id}` هو العرض التحريري: يعيد الدرس **مع كل أسئلته ومفاتيحها**
  (`correctIndex`, `acceptedAnswers`, `explanation`, `choices`).
* `POST .../publish` يعيد `LessonResponse`. يفشل بـ `409` إلا إذا كان الدرس `approved`
  ووحدته `published` بالفعل.
* `DELETE` يتابع أسفل الشجرة: حذف المستوى يحذف وحداته ودروسه وأسئلته.

### 5.4 الأسئلة

| الطريقة | المسار | الدور | ملاحظات |
| --- | --- | --- | --- |
| GET | `/admin/content/questions/{questionId}` | `content_editor` | العرض التحريري لسؤال واحد، بالمفتاح |
| POST | `/admin/content/lessons/{lessonId}/questions` | `content_editor` | `details.problems` قائمة بكل ما يجب إصلاحه |
| POST | `/admin/content/lessons/{lessonId}/questions/bulk` | `content_editor` | `{ "questions": [...], "dryRun": false }` — ذرّي |
| POST | `/admin/content/questions/{questionId}/duplicate` | `content_editor` | `{ "lessonId?", "position?" }`؛ النسخة تأخذ موضعًا جديدًا |
| PATCH | `/admin/content/questions/{questionId}` | `content_editor` | |
| DELETE | `/admin/content/questions/{questionId}` | `content_editor` | `204` |
| POST | `/admin/content/lessons/{lessonId}/questions/reorder` | `content_editor` | `{ "questionIds": [...] }` يجب أن تسرد **كل** أسئلة الدرس مرة واحدة |
| GET | `/admin/content/lessons/{lessonId}/questions/export` | `content_editor` | حزمة بـ `version: 1`، بلا معرّفات ولا مواضع |
| POST | `/admin/content/lessons/{lessonId}/questions/import` | `content_editor` | تقبل الحزمة كما هي، مع `dryRun?` |

الترتيب يعيد `{ items, total }`. قائمة ترتيب ناقصة أو مكرّرة تُرفض بـ `422` يحمل
`details.field = "question_ids"` — وليس `details.problems`. لا ترسل الترتيب في كل ضغطة سهم؛
أرسله بعد اكتمال السحب والإفلات، لكامل المعرّفات دفعة واحدة.

#### الإنشاء الدفعي والاستيراد

`bulk` و`import` **ذرّيان**: تُفحص كل العناصر قبل كتابة أي صف. عند وجود خطأ يعيد الخادم `422`
مع `details.failures` — مصفوفة `{ index, problems }` لكل عنصر فاشل — ولا يتغيّر الدرس إطلاقًا.

| الحالة | الاستجابة |
| --- | --- |
| كتابة ناجحة | `201` و`{ items, total, accepted, dryRun: false }` |
| `dryRun: true` | `200` و`items: []`، و`accepted` هو عدد ما كان سيُكتب |
| عنصر أو أكثر غير صالح | `422` و`details.failures[{index, problems}]` |
| حزمة بإصدار غير معروف | `422` و`details.field = "version"` |

الاستيراد **يُلحق** ولا يستبدل: المواضع تُسند بالترتيب من آخر موضع في الدرس، ولا تعتمد الحزمة
على `position`. استيرادها مرتين يعطي نسختين، لا تعديلًا صامتًا. الحزمة بلا `id`، لذا يمكن
استيرادها في أي درس.

**تطبيع المفتاح:** مفاتيح `fill_blank` و`word_bank` و`multi_select` تُخزَّن بأحرف صغيرة
(`normalise_accepted_answers`). يظهر ذلك في التصدير مهما كتبت الحروف، والمقارنة عند التحقق
غير حسّاسة لحالة الأحرف؛ أما `matching` فيُحفظ كما كتبته، لأن نصفيه يُقارنان مطويّين عند
التصحيح فقط.

`PATCH` دقيق: `correctIndex: null` **قيمة** تُفرّغ المفتاح (لتحويل سؤال اختيار إلى فراغ)، بينما
غياب الحقل يعني «اتركه». نفس المنطق لـ `description` و`imageUrl` و`audioUrl`؛ أما
`title`/`prompt`/`points` فـ `null` معناها «اتركه» وليس «أفرغه».

### 5.5.1 إحصاءات النظرة العامة (admin فقط)

| الطريقة | المسار | الاستعلام | الاستجابة |
| --- | --- | --- | --- |
| GET | `/admin/stats/overview` | `window` من 1 إلى 90، افتراضي 7 | `AdminStatsResponse` |

```json
{ "generatedAt": "2026-10-18T09:00:00Z", "windowDays": 7,
  "users": { "total": 120, "new": 14, "active": 63, "deleted": 2 },
  "learning": { "lessonsCompleted": 410, "sessionsCompleted": 530,
                "answersGraded": 5200, "correctAnswers": 4108,
                "accuracyPercent": 79, "xpAwarded": 9420 },
  "content": { "levels": 6, "units": 42, "lessons": 260,
               "publishedLessons": 214, "drafts": 38, "inReview": 6 } }
```

كل الأرقام مجاميع محسوبة على الخادم (`count`/`sum`)، لا صفوف تُجلب وتُعدّ في المتصفح — فالزمن
لا ينمو مع حجم البيانات إلا بما ينمو به عدد الصفوف المفهرسة.

| الحقل | المعنى الدقيق |
| --- | --- |
| `users.new` | حسابات أُنشئت خلال النافذة |
| `users.active` | حسابات سجّلت دخولًا خلال النافذة (مميّزة، لأن للحساب هويّات متعددة) — ليس شرطًا إكمال درس |
| `users.deleted` | حسابات مغلقة (حذف منطقي) ضمن الإجمالي |
| `learning.lessonsCompleted` | صفوف `lesson_progress` بحالة `completed` |
| `learning.sessionsCompleted` | جلسات اختبار مكتملة |
| `learning.accuracyPercent` | `correctAnswers ÷ answersGraded` موزونة بعدد الإجابات، مقرّبة |
| `content.drafts` | مجموع المستويات والوحدات والدروس في `draft` |
| `content.inReview` | نفس المجموع في `review` |

`window` يغيّر `users.active` و`users.new` فقط؛ بقية الأرقام إجمالية ولا تتحرك مع النافذة.

### 5.5 المستخدمون (admin فقط)

| الطريقة | المسار | الجسم | الاستجابة |
| --- | --- | --- | --- |
| GET | `/admin/users?limit=&cursor=` | — | `{ items, total, hasMore, nextCursor }` |
| POST | `/admin/users` | `{ "userId": "…", "roles": ["content_editor"] }` | `AdminUserResponse` |

عنصر المستخدم: `{ id, email, status, roles, createdAt }`.
الأدوار المسموح إسنادها: `user, support, content_editor, content_reviewer, admin` (قائمة غير
فارغة). الإرسال **يستبدل** المجموعة كاملة، فاحسب القائمة النهائية في الواجهة قبل الإرسال.

### 5.6 الوسائط (لصور وأصوات الأسئلة)

| الطريقة | المسار | الجسم | الاستجابة |
| --- | --- | --- | --- |
| POST | `/media/presign` | `{ "kind": "image", "contentType": "image/png" }` — و`kind` إحدى `image` أو `audio` أو `avatar` | `{ mediaKey, uploadUrl, requiredHeaders, contentType, kind, expiresInSeconds }` |
| POST | `/media/confirm` | `{ "mediaKey": "…" }` | `{ id, kind, contentType, sizeBytes, checksumSha256, url, expiresInSeconds, createdAt }` |
| GET | `/media/{mediaId}` | — | `MediaResponse` |

الرفع لا يمرّ عبر الـ API: `PUT` مباشر إلى `uploadUrl` بترويسات `requiredHeaders` حرفيًا (أي
اختلاف في `Content-Type` = `403` من التوقيع)، ثم `confirm`. الأنواع الفعلية تُعرف من البايتات:
صور 5 ميجابايت كحد أقصى، صوت 10 ميجابايت.

**تنبيه:** الرفع يعمل فقط عند تهيئة S3 (`S3_ENDPOINT` + `S3_ACCESS_KEY_ID` + المفتاح السري).
بدون ذلك يفشل `presign`؛ استخدم في هذه الحالة مسار الملفات المرفقة مع الإصدار تحت `/media/`
(ملفات ثابتة تُخدَّم من نفس الأصل، ومناسبة للمحتوى المنشور لا لرفع المحرّر).

### 5.7 قراءة عامة (معاينة ما يراه المتعلّم)

| الطريقة | المسار | ملاحظات |
| --- | --- | --- |
| GET | `/content/tree` | المنهج **المنشور** فقط: مستويات → وحدات → دروس |
| GET | `/content/lessons/{lessonId}` | درس منشور مع أسئلته بدون مفاتيح — استخدمه لزر «معاينة» |
| GET | `/health` | حياة العملية فقط: `{ data: { status, version }, meta: {} }` |
| GET | `/ready` | فحص القاعدة والكاش؛ `503` عند التدهور |

هذان المساران العامّان يخدمان `ETag`، لذا يمكن للوحة أن تعرض «معاينة المتعلّم» من الكاش.

---

## 6. نموذج البيانات والقواعد

### 6.1 دورة حياة المحتوى

```
draft ──▶ review ──▶ approved ──▶ published ──▶ archived
  │          │            │                        │
  │◀─────────┘            │                        │
  │                       └────▶ review            │
  └────────────────────────────────────────────────┘ (archived → draft)
```

الانتقالات المسموحة حرفيًا:

| من | إلى |
| --- | --- |
| `draft` | `review`, `archived` |
| `review` | `draft`, `approved`, `archived` |
| `approved` | `published`, `review`, `archived` |
| `published` | `archived` |
| `archived` | `draft` |

**نتيجة عملية للواجهة:** تعديل درس منشور يتطلّب أولًا `archived` ثم `draft` ثم التعديل ثم إعادة
النشر. اعرض هذا كزر «سحب من النشر» بدل أن تترك المحرّر يكتشف `409`.

**قواعد النشر:**

1. المستوى لا يشترط أبًا.
2. الوحدة تُنشر فقط بعد نشر مستواها (`409` وإلا).
3. الدرس يُنشر فقط بعد نشره وحدته (`409`)، وفقط من حالة `approved`.

### 6.2 أنواع الأسئلة وقاعدة الصحة لكل نوع

الشرح (`explanation`) إلزامي وغير فارغ في **كل** الأنواع — بدونه لا يتلقى المتعلّم تغذية راجعة.
`points` من 1 إلى 100. `prompt` 1..2000. `imageUrl`/`audioUrl` إمّا `null` أو رابط
`http`/`https` غير فارغ. `position` ≥ 1 وفريد داخل الدرس.

| النوع | `choices` | `acceptedAnswers` | `correctIndex` | قيود إضافية |
| --- | --- | --- | --- | --- |
| `multiple_choice` | 2..6 | فارغة | إلزامي، داخل نطاق الخيارات | — |
| `true_false` | بالضبط 2 | فارغة | 0 أو 1 | عادةً `["True","False"]` |
| `multi_select` | 2..6 | إجابتان صحيحتان على الأقل | **يجب أن يكون `null`** | كل إجابة صحيحة موجودة نصًّا بين الخيارات |
| `word_bank` | 2..6 (بنك الكلمات) | إجابة واحدة على الأقل | `null` | كل إجابة مقبولة موجودة بين البلاطات؛ المشتّتات مسموحة |
| `ordering` | 2..6 بالترتيب الصحيح | **فارغة** | `null` | ترتيب العناصر نفسه هو المفتاح |
| `matching` | 2..6 (عمود الأيمن) | 2..4 أزواج `"left => right"` | `null` | لا تكرار في أي من الجهتين، كل `right` موجود بين الخيارات، والخيارات نفسها بلا تكرار |
| `fill_blank` | **فارغة** | واحدة على الأقل | `null` | لا خيارات إطلاقًا |

الانتهاك يعيد `422` برسالة «The question is not answerable as written.» و
`details.problems` قائمة نصوص — اعرضها فوق النموذج أو اربطها بالحقول.

المقارنة بين `acceptedAnswers` و`choices` **غير حسّاسة لحالة الأحرف** في `multi_select`
و`word_bank`، لأن المفتاح يُطبَّع إلى أحرف صغيرة عند التخزين. اكتب «Hello» أو «hello» — كلاهما
مقبول، والمخزَّن هو الصيغة المطوية.

### 6.3 القيم المسموحة

```
CEFRLevel      : A1 A2 B1 B2 C1 C2
ContentState   : draft review approved published archived
QuestionKind   : multiple_choice multi_select true_false fill_blank word_bank ordering matching
SkillKind      : vocabulary grammar listening reading writing pronunciation
UserRole       : user support content_editor content_reviewer admin
UserStatus     : active pending_verification suspended deleted
MediaKind      : avatar image audio
```

`SkillKind` إلزامي في إنشاء السؤال، وله قائمة منسدلة في النموذج.

---

## 7. الشاشات

ست شاشات تكفي. لكل شاشة: ما تُظهره، من أي مسار، وأفعالها.

### 7.1 الدخول

* الحقول: اسم المستخدم، كلمة المرور.
* الفعل: `POST /auth/dev-login` ثم `GET /me`.
* حالات: `401 AUTH_INVALID_CREDENTIALS` → «بيانات غير صحيحة»؛ `429` → «حاول بعد دقيقة»؛
  `404` → المسار غير مُمكَّن (`DEV_LOGIN_ENABLED=false`) → رسالة صريحة بذلك.
* بعد النجاح: احفظ `accessToken`/`refreshToken`، واعرض الاسم والأدوار في الشريط العلوي.

### 7.2 نظرة عامة

بطاقات صغيرة، ولا شيء غير مدعوم ببيانات حقيقية:

كل البطاقات تأتي من طلب واحد: `GET /admin/stats/overview?window=7` (القسم 5.5.1).

| البطاقة | الحقل |
| --- | --- |
| حالة الخدمة | `/health` و`/ready` مباشرة (خارج الإحصاءات) |
| إجمالي الحسابات | `users.total` |
| الجدد خلال النافذة | `users.new` |
| النشطون خلال النافذة | `users.active` |
| الدروس المكتملة | `learning.lessonsCompleted` |
| الدقة | `learning.accuracyPercent` (والمقام `answersGraded`) |
| المستويات / الوحدات / الدروس | `content.levels` / `content.units` / `content.lessons` |
| مسودات وبانتظار المراجعة | `content.drafts` / `content.inReview` |

الإحصاءات لوحة `admin` فقط؛ المحرّر يرى `403` ويجب أن تُخفى عنه البطاقات كلها بدل إظهار أصفار.
لا تعرض النسب في هذه الصفحة إذا كان `learning.answersGraded = 0` — «0%» هنا ليست دقة، بل دليل
على عدم وجود إجابات بعد.

### 7.3 شجرة المنهج

* جلب: `GET /admin/content/levels` ثم لكل مستوى `.../units` ثم لكل وحدة `.../lessons`.
  ثلاث طبقات من الطلبات — نفّذها بالتدريج (المستوى عند التوسيع) لا دفعة واحدة.
* كل صف: العنوان، الموضع، شارة الحالة، عدد العناصر، أزرار (تعديل، نشر، حذف).
* شارة الحالة بألوان ثابتة: `draft` رمادي، `review` أزرق، `approved` بنفسجي، `published` أخضر،
  `archived` أحمر باهت.
* حذف مستوى/وحدة/درس: نافذة تأكيد تذكر أن الحذف يتابع للأسفل. استخدم `DELETE` ثم أعد الجلب
  من الأب.

### 7.4 نموذج المستوى/الوحدة

حقول مطابقة للـ API حرفيًا، مع `position` كرقم يبدأ من 1 ومؤشر «مشغول» (سيُرفض بـ `409` إن
كان محجوزًا). قائمة الحالة تعرض الانتقالات المسموحة من الحالة الحالية فقط.

### 7.5 نموذج الدرس + الأسئلة

ثلاث مناطق في شاشة واحدة:

1. **بيانات الدرس**: `title`, `summary`, `estimatedMinutes`, `position`, `state`.
2. **الأسئلة**: قائمة مرتّبة، كل عنصر يعرض النوع والمهارة ومقتطف `prompt`، مع أزرار
   (تعديل، نسخ، حذف، أعلى/أسفل). الترتيب يُرسل مرة واحدة إلى `questions/reorder` بكامل
   المعرّفات بعد أي سحب وإفلات. في رأس القسم: «تصدير الحزمة» (نزول JSON)، «استيراد»
   (لصق حزمة أو اختيار ملف → `dryRun` أولًا → تأكيد)، و«إضافة عدة أسئلة» التي تفتح محرّرًا
   بلصق JSON أو CSV وتحوّله إلى `questions/bulk`.
3. **محرّر السؤال**: حقول ديناميكية حسب `kind` وفق جدول 6.2. عند تغيير النوع، امسح الحقول
   التي لا تنتمي إليه (خصوصًا `correctIndex` و`choices`) حتى لا ترسل قيمة يرفضها الخادم.

أزرار الحالة: «إرسال للمراجعة» (`review`)، «اعتماد» (`approved`)، و«نشر» لصاحب
`content_reviewer` فقط.

### 7.6 المستخدمون والأدوار

* قائمة: `GET /admin/users` مع `total` في العنوان.
* لكل صف: البريد، الحالة، الأدوار الحالية، تاريخ الإنشاء، وزر «تعديل الأدوار».
* نافذة التعديل: صناديق اختيار للأدوار، ثم `POST /admin/users` بـ `{ userId, roles }` كاملة.
* حماية من الخطأ: منع إزالة `admin` الأخير من نفسك دون تأكيد صريح.

---

## 8. تدفقات التنفيذ

### 8.1 نشر درس

```
1. PATCH /admin/content/levels/{id} {state:"published"}        (مرة واحدة لكل مستوى)
2. PATCH /admin/content/units/{id}  {state:"published"}        (بعد نشر المستوى)
3. PATCH /admin/content/lessons/{id}{state:"review"}
4. PATCH /admin/content/lessons/{id}{state:"approved"}
5. POST  /admin/content/lessons/{id}/publish                    (content_reviewer)
```

أي خطوة تُرجع `409`: أعد جلب العنصر واعرض السبب («الوحدة غير منشورة»، «الانتقال غير مسموح»).
لا تُعِد المحاولة تلقائيًا.

### 8.2 إضافة سؤال أو عدة أسئلة

```
POST /admin/content/lessons/{id}/questions       → 201 أو 422 مع details.problems
POST /admin/content/lessons/{id}/questions/bulk  → 201، أو 422 مع details.failures[{index,problems}]
```

عند النجاح أعد جلب الدرس (أو أضف العناصر محليًا من الاستجابة) لأن `position` قد يتغيّر. في
الدفعة، اعرض لكل عنصر فاشل رقمَه الأصلي (`index` يبدأ من الصفر) وليس موضعه في القائمة المفلترة.

### 8.2.1 تصدير واستيراد مجموعة أسئلة

```
GET  /admin/content/lessons/{id}/questions/export   → { version, lessonId, lessonTitle, exportedAt, questions }
POST /admin/content/lessons/{id}/questions/bulk     { questions: [...], dryRun: true }   ← معاينة
POST /admin/content/lessons/{id}/questions/import   { version, questions }              ← كتابة
```

احفظ الحزمة كما وصلت؛ لا تعِد بناء الحقول. الإصدار `1` هو المدعوم اليوم، وأي إصدار آخر
مرفوض بـ `422` قبل أن يُكتب شيء. زر «استيراد» يجب أن يمرّ بـ `dryRun` أولًا ويعرض عدد ما
سيُقبل، لأن الاستيراد يضيف ولا يستبدل.

### 8.3 رفع صورة لسؤال

```
POST /media/presign  {kind:"image", contentType:"image/png"}
PUT  uploadUrl       (ترويسات requiredHeaders حرفيًا)
POST /media/confirm  {mediaKey}
→ ضع media.url أو المسار الثابت في حقل imageUrl للسؤال
```

`url` الموقّع قصير العمر (15 دقيقة افتراضيًا). للأسئلة المنشورة استخدم رابطًا ثابتًا تحت
`/media/...` بدل الرابط الموقّع، وإلا انكسرت الصورة في التطبيق بعد ربع ساعة.

---

## 9. قبول العمل (Acceptance criteria)

يُعتبر التنفيذ مكتملًا فقط عند تحقّق كل بند:

- [ ] تسجيل الدخول بـ `admin/admin` يعمل ويعرض الأدوار القادمة من `/me`.
- [ ] انتهاء الرمز يُحدَّث صامتًا مرة واحدة ثم يُعيد الطلب، وفشل التحديث يعيد لشاشة الدخول.
- [ ] شجرة المنهج تجلب المستويات والوحدات والدروس وتعرض الحالات بألوانها.
- [ ] إنشاء مستوى/وحدة/درس ناجح، و`position` محجوز يعرض `409` مفهومًا.
- [ ] دورة نشر كاملة تنجح بالترتيب، ونشر درس في وحدة مسودة يعرض `409` برسالة صحيحة.
- [ ] حذف مستوى يحذف أبناءه، والشجرة تُحدَّث بعده.
- [ ] إنشاء سؤال من كل نوع من الأنواع السبعة ينجح بجسم صحيح.
- [ ] كل قيد في 6.2 مُطبَّق في الواجهة **وأيضًا** يظهر الخطأ القادم من الخادم عند تجاوزه.
- [ ] إعادة ترتيب الأسئلة يُرسل كل المعرّفات وينجح.
- [ ] دفعة فيها عنصر واحد خاطئ لا تكتب أي صف، وتُظهِر `details.failures[].index` و`problems`.
- [ ] `dryRun` يعرض عدد ما سيُكتب دون تغيير الدرس (أعد جلبه للتأكد).
- [ ] تصدير درس ثم استيراده في درس آخر يعيد بناء نفس الأسئلة بمفاتيحها وبالترتيب نفسه.
- [ ] زر النسخ ينشئ سؤالًا جديدًا بمعرّف مختلف وموضع تالٍ، والأصل لا يتغيّر.
- [ ] بطاقات النظرة العامة تُقرأ من `/admin/stats/overview` في طلب واحد، وتتغيّر `users.active`
  و`users.new` وحدهما عند تغيير `window`.
- [ ] تعديل أدوار مستخدم يستبدل المجموعة، ويظهر `403` عند محاولته بدور أقل من `admin`.
- [ ] كل طلب يضيف `Authorization`، و`401/403/409/422/429` لها معالجة موحّدة في مكان واحد.
- [ ] لا يوجد أي طلب إلى مسار غير موجود في القسم 5 (تحقّق من تبويب الشبكة).
- [ ] `npm run build` (أو ما يعادله) ينجح، ووحدة التحكّم النوعي نظيفة.

---

## 10. الفجوات في الـ API — لا تبنِ شاشة قبل سدّها

هذه ليست اقتراحات تجميلية: كل سطر يمثّل شاشة أو زرًا لا يمكن تنفيذه اليوم دون تلفيق بيانات
على العميل.

| الفجوة | الأثر | الـ endpoint المقترح |
| --- | --- | --- |
| سجلّ التدقيق يُكتب ولا يُقرأ | زر «من عدّل هذا؟» مستحيل | `GET /admin/audit?entityType=&entityId=&limit=&cursor=` → عناصر `{ id, actorId, actorEmail, action, entityType, entityId, summary, changes, createdAt }` |
| لا مسار لتغيير حالة الحساب | لا إيقاف ولا تعليق من اللوحة | `PATCH /admin/users/{id}` → `{ status }` مع مراجعة أثر الجلسات |
| لا حذف حساب من الإدارة | أي طلب حذف يحتاج تدخّلًا يدويًا | `DELETE /admin/users/{id}` (حذف منطقي كـ `PATCH /me`) |
| لا قراءة لشجرة الإدارة في طلب واحد | ثلاث طبقات من الطلبات لكل تحميل | `GET /admin/content/tree?states=` |
| لا نشر جماعي | نشر 30 درسًا = 30 طلبًا | `POST /admin/content/lessons/publish-bulk` بـ `{ lessonIds }` يعيد نتيجة لكل عنصر |
| لا بحث/تصفية في المحتوى | تصفّح عشرات الدروس يدويًا | معامل `q` و`state` على قوائم الإدارة |
| `GET /admin/users` لا يدعم تصفية بالدور أو الحالة | شاشة المستخدمين بلا فرز مفيد | `?role=&status=&q=` |
| `PATCH ... {state:"published"}` متاح لرتبة أدنى من مسار النشر | تجاوز بوابة المراجعة | إزالة `published` من مسار التحديث أو رفع شرطه إلى `content_reviewer` |

**سُدَّت في هذا الإصدار:** إحصاءات النظرة العامة، قراءة سؤال منفرد، نسخ سؤال، إنشاء دفعة
ذرّية، وتصدير/استيراد حزمة أسئلة. لم يبقَ من فجوات الأسئلة شيء تحتاجه شاشة التحرير، وبطاقات
النظرة العامة صارت كلها قابلة للعرض.

**أولوية مقترحة بعد ذلك:** إغلاق تجاوز النشر (أمني) ← قراءة سجل التدقيق ←
`admin/content/tree` (أداء) ← بقية العناصر.

---

## 11. ملاحظات أداء وتجربة

* **جلب الشجرة تدريجيًا**: افتح مستوى عند التوسيع لجلب وحداته، ووحدة لجلب دروسها. لا تجلب
  الكل عند الإقلاع.
* **تخزين مؤقت قصير**: خزّن قوائم الإدارة 15–30 ثانية، وأبطلها بعد أي كتابة على نفس العنصر.
* **تحديث متفائل مرفوض**: لا تطبّق تغيير الحالة محليًا قبل استجابة الخادم. `409` سيكون شائعًا
  أثناء عمل محرّرين متعددين، والتراجع عن تحديث متفائل يبدو كخطأ في الواجهة.
* **معالجة `422` كحالة طبيعية**: لا تعرضها كنافذة خطأ عامة؛ ضع `details.problems` تحت الحقول
  المعنية داخل النموذج.
* **RTL**: الحقول التقنية (المعرّفات، الـ JSON، الروابط) بـ `dir="ltr"`.
* **الجلسة**: لا تعتمد على بقاء الرمز؛ تحقّق من `expiresIn` وجدّد قبل الانتهاء بثوانٍ.

---

## 12. خطة تنفيذ مرحلية

| المرحلة | المخرجات | معيار الإنجاز |
| --- | --- | --- |
| 1 | Scaffold + عميل HTTP + مصادقة + `/me` | دخول وخروج وتحديث صامت |
| 2 | شجرة المنهج + نماذج المستوى/الوحدة/الدرس | إنشاء وتعديل وحذف وعرض الحالات |
| 3 | محرّر الأسئلة لكل الأنواع + قواعد 6.2 | إنشاء كل نوع ورفض غير الصالح |
| 4 | النشر ودورة الحالة | دورة كاملة + رسائل 409 الدقيقة |
| 5 | المستخدمون والأدوار | قائمة وتعديل أدوار |
| 6 | الوسائط والمعاينة | رفع صورة وربطها، ومعاينة `/content/lessons/{id}` |
| 7 | الصقل: أخطاء موحّدة، RTL، تحميلات، قبول كامل | كل بنود القسم 9 |

---

## 13. فحص دخان بـ curl

```bash
BASE=http://192.168.69.67:8000

# 1) حياة الخدمة
curl -s "$BASE/health" | jq .data

# 2) دخول
TOKEN=$(curl -s -X POST "$BASE/api/v1/auth/dev-login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' | jq -r .tokens.accessToken)
test -n "$TOKEN" && test "$TOKEN" != "null" || echo "LOGIN FAILED"

# 3) أنا
curl -s "$BASE/api/v1/me" -H "Authorization: Bearer $TOKEN" | jq '.roles'

# 4) المستويات
curl -s "$BASE/api/v1/admin/content/levels" -H "Authorization: Bearer $TOKEN" | jq '.meta'

# 5) المستخدمون
curl -s "$BASE/api/v1/admin/users?limit=5" -H "Authorization: Bearer $TOKEN" | jq '.total'

# 6) إحصاءات النظرة العامة
curl -s "$BASE/api/v1/admin/stats/overview?window=7" \
  -H "Authorization: Bearer $TOKEN" | jq '{users, learning, content}'

# 7) مرفوض بلا رمز (يجب أن يكون 401 وليس 200)
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/api/v1/admin/content/levels"
```

---

## 14. الملفات المرجعية

* عقد العملاء والكاش والحدود: [client-contract.md](client-contract.md)
* إعداد التطوير والدخول الاجتماعي: [../README.md](../README.md)
* مخطط الـ API الحقيقي: `GET /openapi.json`
* قواعد المحتوى: [../app/modules/content/domain/policy.py](../app/modules/content/domain/policy.py)
* مسارات الإدارة: [../app/modules/admin/presentation/routes.py](../app/modules/admin/presentation/routes.py)
* أنواع الأسئلة والصلاحيات: [../app/core/types.py](../app/core/types.py)
