# دليل التكامل — تطبيق ناتيقو للهاتف

هذا المستند مكتوب لمبرمج الـ Flutter. هدفه أن تعرف **بالضبط** ما تحتاج بناؤه، وما الذي
يعطيك إياه الـ backend، وما الذي **غير موجود بعد** حتى لا تبني شاشة على واجهة وهمية.

كل مثال هنا مأخوذ من تشغيل التطبيق فعلياً، لا من النوايا. القيم مثل `201` و
`AUTH_REFRESH_REUSED` مُقيسة.

> ⚠️ **لقطة مرحلة (snapshot) محدَّثة.** يصف هذا المستند سطح الـAPI الحالي بعد دمج مسارات
> الكتالوج والـquiz والتقدّم: الكتالوج **مساران** (`/content/tree` + `/content/lessons/{id}`)،
> والـquiz **ثلاثة** (بدأ / استئناف / أمر موحَّد عبر `op`)، والتقدّم **قراءتان** (`/progress` +
> `/progress/unlocks`). المراجعة المتباعدة (SRS) لم تبنَ بعد. ومسار الشرح المستقل
> `GET /content/questions/{id}/explanation` **حُذف** — صار الشرح يُعاد مع إجابة الـquiz.
> للمعمارية التفصيلية راجع [`backend-system-ar.md`](backend-system-ar.md) و
> [`mobile-app-architecture.md`](mobile-app-architecture.md).

---

## 1.TL;DR

| السؤال | الجواب |
| --- | --- |
| ماذا يوجد؟ | حسابات + ملفات مستخدم + كتالوج محتوى (قراءة) + محرّك quiz + قراءات تقدّم |
| ماذا لا يوجد بعد؟ | مراجعة SRS، قلوب/إنجازات، اشتراكات، إرسال إشعارات، رفع وسائط، ذكاء اصطناعي، لوحة admin |
| كيف أدخل؟ | Google أو Apple فقط — أرسل ID token إلى `/auth/google` أو `/auth/apple`. **لا كلمة مرور في النظام** |
| كيف أُنشئ حساباً؟ | أول دخول يُنشئ الحساب تلقائياً — لا يوجد `register` ولا `forgot-password` |
| كيف أدخل بعد ذلك؟ | access token **15 دقيقة** + refresh token **30 يوم** مع تدوير |
| ماذا أعرض قبل الدخول؟ | الكتالوج كاملاً — **مساران** عامّان (`/content/tree` + `/content/lessons/{id}`) |
| هل أستطيع حل الأسئلة؟ | **نعم** — محرّك الـquiz يصحّح على السيرفر ويعيد الصحّ + الشرح لكل إجابة |
| أهم فخّين | لا ترسل `cursor` فارغاً · لا تُهمل single-flight لتحديث التوكن |

---

## 2. اقرأ هذا أولاً: ما لا يوجد

هذه أهم نسبة في المستند. الـ backend حالياً **قاعدة محتوى وحسابات + محرّك quiz + قراءات تقدّم**.

```
✅ موجود                          ❌ غير موجود (أي طلب لها يعيد 404)
─────────────────────────────      ─────────────────────────────
POST   /auth/google                أي مراجعة SRS / بطاقات
POST   /auth/apple                 أي إنجازات / لوحة صدارة
POST   /auth/refresh               أي اشتراك أو دفع
POST   /auth/logout                أي إرسال إشعارات (فقط التفضيلات)
GET    /me · PATCH /me             أي رفع صوت أو صورة (لا يوجد upload)
DELETE /me                         أي تحليلات
GET    /me/devices                 أي ذكاء اصطناعي / تصحيح كتابة
DELETE /me/devices/{id}            ─────────────────────────────
GET    /content/tree
GET    /content/lessons/{id}
POST   /quiz/sessions
GET    /quiz/sessions/{id}
POST   /quiz/sessions/{id}
GET    /progress
GET    /progress/unlocks
```

**ماذا يعني هذا عملياً:** تقدر تبني شاشات تسجيل/دخول/تصفّح الدروس، وقراءة الأسئلة، **وحلّها**
(الـquiz يصحّح على السيرفر ويعيد الشرح)، **ومتابعة تقدّمك** (شاشة "تقدّمي" وسجل XP).
**لا تقدر** تبني شاشة "راجع اليوم" (SRS مؤجّلة) ولا لوحة إنجازات — تحتاج backend جديد.

الترتيب المقترح للبناء: الحسابات والكتالوج أولاً، ثم الـquiz والتقدّم (live الآن)، ثم
مراجعة SRS عند بناء المرحلة الثانية.

---

## 3. حالة الـAPI — الجدول الكامل

| # | Method | Path | مصادقة | يجيب بـ |
| --- | --- | --- | --- | --- |
| 1 | `POST` | `/api/v1/auth/google` | — | `200` + مستخدم + توكنات (يُنشئ الحساب أول مرة) |
| 2 | `POST` | `/api/v1/auth/apple` | — | `200` + مستخدم + توكنات (نفس العقد) |
| 3 | `POST` | `/api/v1/auth/refresh` | — (refresh في body) | `200` + توكنات جديدة |
| 4 | `POST` | `/api/v1/auth/logout` | — (refresh في body) | `200` |
| 5 | `GET` | `/api/v1/me` | Bearer | `200` |
| 6 | `PATCH` | `/api/v1/me` | Bearer | `200` |
| 7 | `DELETE` | `/api/v1/me` | Bearer | `204` — إغلاق الحساب |
| 8 | `GET` | `/api/v1/me/devices` | Bearer | `200` + مصفوفة |
| 9 | `DELETE` | `/api/v1/me/devices/{device_id}` | Bearer | `204` |
| 10 | `GET` | `/api/v1/content/tree` | — | `200` + الشجرة كاملة (nest) |
| 11 | `GET` | `/api/v1/content/lessons/{lesson_id}` | — | `200` |
| 12 | `POST` | `/api/v1/quiz/sessions` | Bearer | `201` + الجلسة والأسئلة (بلا مفتاح) |
| 13 | `GET` | `/api/v1/quiz/sessions/{session_id}` | Bearer | `200` + الجلسة والأسئلة |
| 14 | `POST` | `/api/v1/quiz/sessions/{session_id}` | Bearer | `200` + نتيجة الأمر (`op`: answer/complete/abandon) |
| 15 | `GET` | `/api/v1/progress` | Bearer | `200` + الملخّص + صفحة XP |
| 16 | `GET` | `/api/v1/progress/unlocks` | Bearer | `200` + خريطة الدروس |
| 17 | `GET` | `/health` | — | `200` |
| 18 | `GET` | `/ready` | — | `200` / `503` |

كل المسارات تحت `/api/v1/`. **`/health` و `/ready` ليست تحت `/api/v1`.** ووسائل الأسئلة
(`/media/...`) تُقدَّم من الأصل نفسه لكن **ليست تحت `/api/v1`** (ملفات ثابتة — انظر 11).

---

## 4. الاتصال

```dart
// staging
const base = 'https://api.staging.natiqo.app';
const api  = '$base/api/v1';
```

- **HTTPS إجباري** في الإنتاج (`HSTS` مفروض من السيرفر).
- كل التوكنات في الهيدر: `Authorization: Bearer <accessToken>`
- لا ترسل `X-Request-ID` إلا للربط بسجلات السيرفر. السيرفر يولّد واحداً إذا لم ترسله
  ويردّه في نفس الهيدر — استخدمه في تقارير الأخطاء.

---

## 5. المصادقة — الجزء الأهم

### 5.1 التخزين

| التوكن | العمر | أين |
| --- | --- | --- |
| `accessToken` | **900 ثانية (15 دقيقة)** | الذاكرة فقط |
| `refreshToken` | **2 592 000 ثانية (30 يوم)** | تخزين آمن على الجهاز |

> ⚠️ **`refreshToken` في `flutter_secure_storage` فقط.** لا تضعه في
> `shared_preferences` ولا في كود مدمج. هو bearer token صالح 30 يوماً؛ تسريبه يعني سرقة
> الحساب. `accessToken` اختصاراً يمكن إبقاؤه في الذاكرة فقط لأنه قصير العمر.

### 5.2 لا توجد كلمة مرور — الدخول بـ Google أو Apple

`register` و `login` و `forgot-password` و `reset-password` **لم تعد موجودة: كلها تُرجع `404`.**
السيرفر لا يخزّن كلمة مرور لأي حساب، فلا يوجد ما يُسرَّب ولا ما يُستعاد.

الدخول وإنشاء الحساب مسار واحد:

```text
1. SDK على الجهاز   google_sign_in      →  idToken (JWT)
                    sign_in_with_apple  →  idToken (JWT)
2. POST /api/v1/auth/google  { idToken, ... }
3. السيرفر يتحقق من التوقيع والـ issuer والـ audience والصلاحية
4. أول مرة يظهر (المزوّد، subject) → يُنشأ الحساب. بعدها → نفس الحساب دائماً.
```

```jsonc
// POST /api/v1/auth/google   |   POST /api/v1/auth/apple
{
  "idToken": "eyJhbGciOi...",        // إجباري — 16 حرفاً على الأقل
  "displayName": "Ali",              // يُطبَّق فقط عند إنشاء حساب جديد
  "locale": "ar-IQ",                 // en-GB | ar-IQ | en
  "nativeLanguage": "ar",            // en | ar | iq
  "englishVariant": "british",       // british | american | both
  "timezone": "Asia/Baghdad",
  "device": {                        // اختياري لكن مطلوب للإشعارات
    "platform": "ios",               // ios | android | web
    "pushToken": "ea1b2c3d4e5f6a7b", // 8 أحرف على الأقل
    "appVersion": "1.0.0"
  }
}
```

**أول دخول يُنشئ الحساب** — لا يوجد مسار تسجيل منفصل، ولهذا الحالة `200` وليست `201`.

نقاط يجب أن تعرفها:

- **`idToken` هو ID token من المزوّد نفسه، وليس access token ولا authorization code.**
  استخدم `google_sign_in` أو `sign_in_with_apple` مباشرة؛ لا تبنِ تدفق OAuth بنفسك.
- **`displayName` و `locale` و `timezone` تُطبَّق عند إنشاء الحساب فقط.** مستخدم عائد
  يحتفظ بتفضيلاته المخزّنة، فلا يستطيع جهاز قديم أن يعيد ضبطها.
- **Apple لا يضع الاسم داخل التوكن أبداً**، ويرسله مرة واحدة فقط عند أول تصريح. لذلك
  أرسل `displayName` في أول تسجيل، وإلا صار الاسم مقتطعاً من البريد.
- **`emailVerified` الآن يعكس ما يؤكده المزوّد** (Google يؤكده دائماً). لا تعرض شاشة
  "أكّد بريدك" — لا يوجد endpoint لذلك.
- **`currentLevel` يبدأ `A1` تلقائياً.** لا يوجد placement test ولا endpoint يغيّره.
- كل تطبيق يحتاج `client id` مُدرَجاً في `GOOGLE_CLIENT_IDS` / `APPLE_CLIENT_IDS` على
  السيرفر. توكن صادر لـ client id غير مُدرَج يُرفض بـ `401 AUTH_IDENTITY_INVALID`،
  ومزوّد غير مُهيّأ أصلاً يُرجع `401 AUTH_PROVIDER_UNAVAILABLE`.
- حساب بمزوّد ثانٍ (Apple بعد Google) **يُربط بنفس الحساب** إذا أكّد المزوّد أن البريد
  للشخص نفسه وكان مطابقاً. إن كان غير مؤكَّد فالنتيجة `401 AUTH_IDENTITY_CONFLICT` —
  وهذا مقصود: بدونه يستطيع من يملك حساب مزوّد بعنوان غير مؤكّد أن يستولي على الحساب.
- حساب بلا بريد صالح من المزوّد ولا حساب قائم: `401 AUTH_IDENTITY_INCOMPLETE`.

**ما ترده** (`200`):

```jsonc
{
  "user": {
    "id": "bdb4f270-...",
    "email": "ali@example.com",
    "status": "active",
    "roles": ["user"],
    "emailVerified": true,            // يعكسه المزوّد — Google يؤكده دائماً
    "createdAt": "2026-10-02T21:20:18.664792Z",
    "profile": {
      "displayName": "Ali", "avatarKey": null,
      "nativeLanguage": "ar", "englishVariant": "british",
      "timezone": "Asia/Baghdad", "currentLevel": "A1"
    },
    "settings": {
      "locale": "en-GB", "explanationLanguage": "en",
      "dailyGoalMinutes": 30, "dailyGoalNewWords": 10,
      "notificationPreferences": {
        "email": true, "push": true, "dailyReminder": true,
        "streakWarning": true, "reviewDue": true, "weeklyReport": false,
        "quietHoursStart": 22, "quietHoursEnd": 8
      }
    }
  },
  "tokens": {
    "accessToken": "eyJhbGciOi...",
    "refreshToken": "eyJhbGciOi...",
    "tokenType": "Bearer",
    "expiresIn": 900,                  // ثانية
    "refreshExpiresIn": 2592000        // ثانية
  }
}
```

> **انتبه:** `currentLevel` يبدأ `A1` تلقائياً. **لا يوجد placement test ولا أي endpoint
> يغيّره.** لا تعرض للمستخدم "اختبار تحديد مستوى" — لا يوجد.

### 5.3 التحديث — `POST /auth/refresh` `→ 200`

**التوكن يتغير في كل مرة.** هذه ليست تجميلية — انظر 5.5.

```jsonc
{ "refreshToken": "<القديم>", "device": { "platform": "ios" } }
```

اختبرته: التوكن القديم يعطي `401 AUTH_REFRESH_REUSED` بعد الاستخدام.

### 5.4 ⚠️ إعادة استخدام refresh = إبطال كل الجلسات

هذا أهم سلوك أمني في النظام، ويجب أن تستوعبه:

```
1. عميل خدّك refresh جديد  →  القديم أصبح مستخدماً
2. عميل آخر (سابق / مهاجم) يقدّم القديم
3. السيرفر يعتبره سرقة → يُبطل **كل** جلسات المستخدم على كل أجهزته
```

إن وصلتك `AUTH_REFRESH_REUSED` من تطبيقك **بسبب سباق، وليس هجوماً**، فأنت طردت
المستخدم من كل أجهزته. لهذا السبب بُني النمط في 5.5.

### 5.5 النمط الصحيح: `401` → refresh واحد فقط (single-flight)

هذا أهم شيء تبنيه في طبقة الشبكة. بدونه سيحصل سباق عند إطلاق التطبيق.

```dart
class ApiClient {
  Future<bool>? _refreshInFlight;      // ⬅️ السر

  Future<http.Response> send(String method, String path, {Object? body}) async {
    var res = await _raw(method, path, body: body, token: _access);
    if (res.statusCode != 401) return res;

    if (_refreshInFlight == null) {
      _refreshInFlight = _doRefresh().whenComplete(() => _refreshInFlight = null);
    }
    final ok = await _refreshInFlight!;       // كل النداءات تنتظر نفس النتيجة
    if (!ok) { await _signOut(); rethrow; }
    return _raw(method, path, body: body, token: _access);
  }
}
```

**بدون** `Future<bool>? _refreshInFlight`، عند إطلاق التطبيق 5 طلبات متوازية، كل
واحد يحتاج refresh، والأول يدوّر التوكن، والباقي يقدّمون توكنات قديمة → `AUTH_REFRESH_REUSED`
→ طرد كل الجلسات. هذا أسوأ خطأ ممكن في تطبيق جوال.

**نقطة واحدة أخيرة:** أرسل `device` مع طلب الـ refresh، وإلا ستنقطع `pushEnabled` عن
الجهاز. السيرفر يجدّد `lastSeenAt` في كل عملية دخول وفي `refresh`.

### 5.6 الخروج

```jsonc
// طلب
{ "refreshToken": "<refresh>", "allDevices": false }
// استجابة 200
{ "detail": "Signed out." }
```

`allDevices: true` يخرج كل أجهزة المستخدم. بعد الخروج، أي refresh يعطي
`401 AUTH_REFRESH_REUSED` (ليس `AUTH_TOKEN_REVOKED` — انتبه للكود).

### 5.7 إغلاق الحساب — `DELETE /me` `→ 204`

```http
DELETE /api/v1/me
Authorization: Bearer <accessToken>
```

`204` بلا جسم. يُغلق الحساب فوراً ويُبطل كل سلاسل الـ refresh، فيصبح أي `refresh` بعدها
`401`. الـ `accessToken` الحالي يبقى صالحاً حتى انتهائه (15 دقيقة) لكنه يشير إلى حساب
مغلق، فترفضه كل المسارات المحمية بـ `401 USER_DELETED` — امسح التوكنات محلياً فوراً.

الطلب الثاني لنفس الحساب يرجع `204` أيضاً: العملية idempotent، فإعادة المحاولة بعد
انقطاع الشبكة لا تفشل. **لا يوجد تأكيد عبر البريد ولا مهلة سماح.**

---

## 6. الأخطاء

**كل** الأخطاء — بما فيها `422` — ترجع بنفس الشكل:

```jsonc
{
  "error": {
    "code": "AUTH_IDENTITY_INVALID",       // تفرّع عليه، لا على message
    "message": "That sign-in could not be verified. Please try again.",
    "details": {                            // اختياري
      "fields": [
        { "field": "idToken", "issue": "...", "type": "..." }
      ]
    },
    "requestId": "01d7605b12b64d11a7c68a28320587fe"
  }
}
```

> ℹ️ **قاعدة الثقة في `openapi.json`.** كل عملية تحت `/api/v1` تنشر الآن الغلاف أعلاه
> لـ`400/401/403/404/409/422/429/500`، فيمكنك توليد عميل منه مباشرة. (كان `422`
> يُنشر كـ`HTTPValidationError` — شكلاً لا نرسله أبداً؛ أُصلح ذلك، و
> `test_the_schema_describes_the_error_envelope_we_actually_send` يمنع رجوعه.)
> استخدمه للأشكال، واستخدم هذه الوثيقة للسلوك.

### جدول الأخطاء — ماذا تفعل

| `code` | HTTP | ماذا يعني | ماذا تفعل |
| --- | --- | --- | --- |
| `AUTH_IDENTITY_INVALID` | 401 | التوكن لم يُتحقق منه (توقيع/audience/issuer/صلاحية) | أعد المحاولة عبر SDK المزوّد، ثم "تعذّر الدخول" |
| `AUTH_PROVIDER_UNAVAILABLE` | 401 | المزوّد غير مُهيّأ على السيرفر | أخفِ زر هذا المزوّد — أعلم الفريق الخلفي |
| `AUTH_IDENTITY_CONFLICT` | 401 | بريد موجود لكن المزوّد لم يؤكّده | "سجّل الدخول بالطريقة التي استخدمتها سابقاً" |
| `AUTH_IDENTITY_INCOMPLETE` | 401 | المزوّد لم يشارك بريداً صالحاً | اطلب من المستخدم حساباً آخر |
| `AUTH_ACCOUNT_DISABLED` | 403 | الحساب معلّق | اعرض رسالة "تواصل معنا" |
| `USER_DELETED` | 401 | الحساب مُغلق | امسح التوكنات محلياً واخرج |
| `AUTH_TOKEN_INVALID` | 401 | التوكن تالف | شغّل 5.5 |
| `AUTH_TOKEN_EXPIRED` | 401 | انتهت صلاحية الـ access | شغّل 5.5 |
| `AUTH_REFRESH_INVALID` | 401 | refresh غير صالح | اخرج من الحساب |
| `AUTH_REFRESH_REUSED` | 401 | إعادة استخدام — **كل الجلسات أُبطلت** | اخرج فوراً، لا تحاول مرة أخرى |
| `AUTH_FORBIDDEN` | 403 | لا صلاحية | لا تعرض المحتوى |
| `VALIDATION_ERROR` | 422 | حقل خاطئ | `details.fields[].field` يحدد أي حقل |
| `LESSON_NOT_FOUND` / `QUESTION_NOT_FOUND` | 404 | غير موجود أو غير منشور | "المحتوى غير متاح" |
| `DEVICE_NOT_FOUND` | 404 | جهاز غير موجود | حدّث القائمة |
| `PAYLOAD_TOO_LARGE` | 413 | payload كبير | `details` فيه `max_bytes` |
| `RATE_LIMITED` | 429 | تجاوزت الحد | انتظر `Retry-After` (**انظر 9**) |
| `INTERNAL_ERROR` | 500 | خطأ سيرفر | اعرض رسالة عامة + `requestId` |

### 422 مقابل 404 — الفرق مقصود

```
GET /content/lessons/not-a-uuid        → 422 VALIDATION_ERROR  (ليس UUID إطلاقاً)
GET /content/lessons/<uuid غير منشور>  → 404 LESSON_NOT_FOUND
```

**UUID** مشوّه يُرفض كـ validation، لأنه لا يمكن أن يكون id أصلاً. أما UUID صحيح لا يشير
إلى درس منشور فيُعامل مثل غير المنشور عمداً، حتى لا يُستخدم الـ endpoint لكشف محتوى غير
منشور.

**القاعدة:** `404` = "اطلب من المستخدم شيئاً آخر"، `422` = "طلبك مبني خطأ". لا تعِد
التجربة على `404`.

---

## 7. القواعد العامة

### 7.1 camelCase

```jsonc
// استجابة — camelCase دائماً
{ "displayName": "Ali", "dailyGoalMinutes": 30 }
// طلب — الاثنان مقبولان
{ "displayName": "Ali" }     // ✅ الموصى به
{ "display_name": "Ali" }    // ✅ مقبول (ترحيل)
```

**ارسل camelCase.** snake_case مقبول للترحيل فقط وليس للعقد النهائي.

⚠️ **الحقول غير المعروفة تُرفض بـ 422، لا تُتجاهل.** هذا مقصود:

```
PATCH /me  { "currentLevel": "B2" }
→ 422  details.fields = [{ "field": "currentLevel",
                           "type": "extra_forbidden" }]
```

هذه الحماية تمنع المستخدم من تخطي placement test. **لا تعِد إرسال `currentLevel` من
واجهة تعديل الملف الشخصي.**

### 7.2 ⚠️ `cursor` يجب أن يُحذف تماماً

**الفخ رقم واحد في هذا المشروع.** معظم عملاء HTTP يحوّلون `null` إلى `cursor=`
فارغ، والسيرفر يرفضه:

```
GET /progress?limit=1&cursor=          → 422 VALIDATION_ERROR
   "The cursor is not usable. Start again from the first page."
GET /progress?limit=1&cursor=garbage   → 422 VALIDATION_ERROR
GET /progress?limit=1                  → 200 ✅
```

```dart
// ❌ خطأ — يرسل ?cursor=
final q = {'limit': 20, if (cursor != null) 'cursor': cursor};
// ✅ صحيح
final q = <String, String>{'limit': '20'};
if (cursor != null && cursor.isNotEmpty) q['cursor'] = cursor;
```

### 7.3 الترقيم (cursor)

```jsonc
{
  "items": [ /* ... */ ],
  "meta": { "limit": 20, "hasMore": true, "nextCursor": "eyJ2Ijox..." }
}
```

- `nextCursor` **نص مشفّر — لا تحلله ولا تبنيه ولا تحفظه** بين نسخ التطبيق.
- أرسله حرفياً كما ورد.
- توقف عند `hasMore == false` أو `nextCursor == null`.
- `limit` من 1 إلى 100 (الافتراضي 20). أكبر من 100 = `422`.
- **لا يوجد `offset`** عن قصد: الترقيم بالـ offset يفوّت أو يكرر صفوفاً إذا تغيّر
  المحتوى أثناء التمرير.

### 7.4 مسارات بشرطة مائلة أخيرة

```
GET /api/v1/content/tree/   →  307 → /api/v1/content/tree?limit=1
```

`307` يحافظ على query string. **ابنِ المسارات بدون شرطة أخيرة**، وتأكد أن عميلك يرسل
الـ query عند التحويل التلقائي، وإلا ضاع `limit` و`cursor` وعدت لصفحة واحدة.

### 7.5 ضغط GZip

الاستجابات فوق **400 بايت** تُضغط إذا كان العميل يرسل `Accept-Encoding: gzip`.
`http` في Flutter يتعامل معها شفافة — لا تعمل شيئاً.

### 7.6 الرؤوس القادمة دائماً

`X-Content-Type-Options: nosniff` · `Referrer-Policy` · `Content-Security-Policy` ·
`Permissions-Policy` (+ `Strict-Transport-Security` في الإنتاج).

`nosniff` تعني: **لا تستخدم `eval` على أي response body.**

### 7.7 CORS (للويب/webview فقط)

إن كان تطبيقك يمر عبر webview أو متصفح، origins المسموحة محدودة. الهيدر
`Access-Control-Expose-Headers` يسمح بقراءة `ETag` و`Cache-Control` — بدونه لن تستطيع
الـ revalidation في الويب بينما تعمل في iOS/Android. هذا فرق صامت.

---

## 8. الكاش — الكتالوج فقط

الكتالوج هو الشيء الوحيد القابل للكاش. **كل شيء آخر `no-store`** — بما فيه كل ردود
المصادقة، حتى لا يبقى ملف المستخدم في كاش HTTP على جهاز مشترك.

الرد القابل للكاش:

```
Cache-Control: public, max-age=60, stale-while-revalidate=600
ETag: "b1d0c659d85275a94051d06b4702d1f1"
Vary: Accept-Encoding, Origin
```

الحلقة المطلوبة:

```
1. GET → احفظ الجسم + الـ ETag
2. عند كل إطلاق: If-None-Match: "<etag>"
3. 304 → استخدم المحفوظ. نفس Cache-Control، فالصلاحية تتجدد كل دقيقة
   (مرة كل دقيقة، لا مرة كل إطلاق)
```

**لا تخزّن 404.** معناه "غير منشور الآن" لا "لن يكون أبداً" — تخزينه يخفي المحتوى فور
نشره.

`ETag` يختلف بين gzip و identity. **احفظ الوسم مع الترميز الذي استلمته فعلاً.**

الكتالوج **يعمل بدون إنترنت** بعد أول تحميل بهذه الآلية.

---

## 9. تحديد المعدل

لكل client IP، لكل scope، لكل دقيقة:

| Scope | الافتراضي |
| --- | --- |
| Auth (`/auth/*`) | **10 / دقيقة** |
| الكتالوج (`/content/*`) | **240 / دقيقة** |
| الباقي | 120 / دقيقة |

```
429 Too Many Requests
Retry-After: 60
```

**10 طلبات في الدقيقة على `/auth/*` صارمة.** موثّق: هذا يمنع credential stuffing من
استنزاف ميزانية مستخدم عادي. لكن عميلاً يسجّل دخوله بأخطاء typoes سيصطدم بالحد. تعامل
معه:

- `429` **ليس خطأ قاتل** — إشارة إيقاع.
- انتظر `Retry-After` ثانية على الأقل، ولا تكرر فوراً.
- لا تعرض "خطأ في الشبكة" — اعرض "طلبات كثيرة، حاول بعد قليل".
- استخدمه كمؤشر على bug عندك: 10 طلبات/دقيقة يعني حلقة إعادة محاولة.

---

## 10. تفاصيل كل endpoint

### `GET /api/v1/me` — الملف الشخصي
Bearer. `200` = `MeResponse`. استخدمه عند إقلاع التطبيق للتأكد من حالة الجلسة.

### `PATCH /api/v1/me` — التعديل
Bearer. PATCH semantics: **الحقل المحذوف لا يُلمس**، لا يُصفَّر.

```jsonc
{ "displayName": "Ali H.", "dailyGoalMinutes": 45,
  "notificationPreferences": { "push": false } }   // جزئي — لا ترسل الكل
```

`notificationPreferences` يقبل جزئياً (`email`/`push`/`dailyReminder`/`streakWarning`/
`reviewDue`/`weeklyReport` + `quietHoursStart`/`quietHoursEnd` كعدد 0–23).

يُرجع `MeResponse` كاملاً. **لا ترسل `currentLevel`** (→ 422).

### `GET /api/v1/me/devices` — الأجهزة
Bearer. `200` + مصفوفة:

```jsonc
[{ "id": "35c07d96-...", "platform": "android",
   "appVersion": "2.1.0", "lastSeenAt": "2026-10-02T21:20:50.833914Z",
   "pushEnabled": true }]
```

### `DELETE /api/v1/me/devices/{device_id}` — إخراج جهاز
Bearer. `204` (بلا جسم).

---

## 11. الكتالوج — مساران

### `GET /content/tree` — شجرة المنهج كاملة

ردّ واحد **غير مصفّح** يعيد الهيكل كله: مستويات ← وحدات ← دروس، مع العدّادات.

```jsonc
{
  "items": [
    { "id": "5601a155-...", "code": "A1", "title": "A1 Starter",
      "description": "Greetings, introductions...", "position": 1, "unitCount": 24,
      "units": [
        { "id": "...", "levelCode": "A1", "title": "...", "description": null,
          "position": 1, "lessonCount": 4,
          "lessons": [
            { "id": "...", "unitId": "...", "title": "...", "summary": "...",
              "position": 1, "estimatedMinutes": 5, "questionCount": 6 }
          ] }
      ] }
  ]
}
```

`code` هو CEFR (حروف كبيرة). المستويات المنشورة `A1` حتى `C2`. العدّادات (`unitCount`,
`lessonCount`, `questionCount`) تأتي محسوبة — اعرضها مباشرة ولا تحسبها. لا يوجد ترقيم:
الردّ كامل، والمحتوى المنشور هو الحدّ.

### `GET /content/lessons/{lesson_id}` — الدرس الكامل

```jsonc
{
  "id": "...", "unitId": "...", "title": "...", "summary": "...",
  "position": 1, "estimatedMinutes": 5, "questionCount": 2,
  "questions": [
    { "id": "...", "kind": "multiple_choice", "skill": "vocabulary",
      "prompt": "Choose the correct word.", "points": 1,
      "choices": ["greeting", "greater", "grounded"], "prompts": [],
      "imageUrl": "/media/images/<question-id>.jpg",
      "audioUrl": "/media/audio/<question-id>.mp3" },
    { "id": "...", "kind": "fill_blank", "skill": "grammar",
      "prompt": "She ___ from Baghdad.", "points": 2, "choices": [], "prompts": [],
      "imageUrl": null, "audioUrl": null },
    { "id": "...", "kind": "matching", "skill": "vocabulary",
      "prompt": "Match each number to the word for it.", "points": 1,
      "choices": [], "prompts": ["1", "2", "3", "4"],
      "imageUrl": null, "audioUrl": null }
  ]
}
```

`kind`: `multiple_choice` · `multi_select` · `true_false` · `fill_blank` · `word_bank` · `ordering` · `matching`
`prompts`: العناصر المجرّدة المطلوب توصيلها — تُملأ لسؤال `matching` وحده، و`[]` لكل نوع آخر.
`skill`: `vocabulary` · `grammar` · `listening` · `reading` · `writing` · `pronunciation`

> `imageUrl` و`audioUrl` وسيطتان اختياريتان تُعرضان مع السؤال قبل الإجابة (توضيح/لفظ).
> القيمة إما `null` أو **مسار نسبي على نفس أصل الـAPI** بصيغة `/media/...`. يتغيّر الاسم
> `<question-id>` مع كل سؤال. الوسائط ملفات ثابتة (immutable) تُقدَّم من `/media` لا من
> `/api/v1`، فخزّنها محلياً لعمر التطبيق. لا تُبنَ على الوسيلة أي دلالة للإجابة.

> ⚠️ **الأسئلة بدون مفتاح إجابة في حمولة الدرس.** لا يوجد `correctIndex` ولا
> `acceptedAnswers` ولا `explanation` — عمداً، حتى يمكن قراءة الدرس قبل تسجيل الدخول.
> **الشرح ليس على سطح الكتالوج إطلاقًا**؛ يُعاد فقط مع إجابة الـquiz.
>
> **النتيجة المباشرة:** **لا يمكنك تصحيح إجابة المستخدم من الكتالوج.** أي شاشة "اختبرني"
> على الكتالوج تعرض الأسئلة فقط، **ولا تستطيع تحديد الصح والخطأ**، ولا حفظ النتيجة. للتصحيح
> والشرح استخدم محرّك الـquiz
> (انظر [`mobile-app-architecture.md`](mobile-app-architecture.md) §6.3).

> ⚠️ **`ordering` لا يعرض `choices` في الكتالوج.** ترتيب `choices` المخزَّن هو الترتيب الصحيح
> نفسه، فإظهاره على سطح مشترك ومُخزَّن مؤقتًا يسرّب الجواب. لذلك يعيد الكتالوج `choices: []`
> لسؤال `ordering`؛ القائمة القابلة للسحب (بترتيب مبعثر لكل محاولة) تأتي فقط من جلسة الـquiz.

> ⚠️ **`matching` يعرض `prompts` ولا يعرض `choices` في الكتالوج.** العناصر المطلوب توصيلها
> (`prompts`) عامة — معرفة أن `Dog` تُوصَل بشيء لا تقول بشيء — بينما بنك الخيارات يُقرأ
> جنبًا إلى جنب معها، وترتيبه المخزَّن هو ترتيب الأزواج نفسه، فإظهاره على سطح مشترك ومُخزَّن
> مؤقتًا يسرّب التوصيل. لذلك يعيد الكتالوج `choices: []` لسؤال `matching`، ويأتي البنك
> (مبعثرًا بحيث لا يقف خيار أمام ما يجيبه) من جلسة الـquiz فقط.

### مسار الشرح المستقل — **حُذف**

كان هناك `GET /content/questions/{question_id}/explanation` يعيد `{ id, explanation }`،
وقد **أُزيل** من الـAPI: لا يكشف الشرح أي سطح كتالوج. صار الشرح جزءًا من استجابة إجابة
الـquiz (`POST /quiz/sessions/{id}` بأمر `{"op":"answer"}`) مصحوبًا بالتصحيح، فلا لزوم
لمسار منفصل.

> تفاصيل محرّك الـquiz (`start` / `resume` / الأمر الموحَّد `op`) وقراءتَي التقدّم
> (`/progress` + `/progress/unlocks`) في
> [`mobile-app-architecture.md`](mobile-app-architecture.md) §6.3–§6.4.

---

## 12. قائمة المهام

### طبقة الشبكة
- [ ] `ApiClient` مع base URL و`Authorization` تلقائي
- [ ] single-flight refresh (§5.5) — **الأهم**
- [ ] `X-Request-ID` في كل log
- [ ] `cursor` يُحذف عند null (§7.2)
- [ ] GZip شفاف
- [ ] التقاط `Retry-After` على `429`

### التخزين
- [ ] `refreshToken` في `flutter_secure_storage`
- [ ] `accessToken` في الذاكرة فقط
- [ ] كاش محلي للكتالوج: ETag + body
- [ ] تخزين `deviceId` عند تسجيل الجهاز

### الحالة
- [ ] آلة حالة: ضيف / مُصادَق / منتهي
- [ ] تفضيلات الإشعارات من `/me` (لا تخترنها محلياً كمصدر حقيقة)
- [ ] `GET /me` عند الإقلاع

### الشاشات
- [ ] دخول بـ Google و Apple، مع عرض `details.fields` عند 422
- [ ] إنشاء الحساب تلقائياً عند أول دخول (لا شاشة تسجيل منفصلة)
- [ ] تصفح: مستويات → وحدات → دروس
- [ ] درس بأسئلة قابلة للعرض (بدون تصحيح)
- [ ] (أُزيل) كان الشرح يُجلب من `/content/questions/{id}/explanation` — صار مع إجابة الـquiz
- [ ] الملف الشخصي (بدون حقل المستوى)
- [ ] إدارة الأجهزة

### مطلوب قرار قبل البناء
- [ ] **تصحيح الأسئلة + حفظ التقدم** — **مبنيّ الآن**: محرّك quiz (`/api/v1/quiz/*`) وقراءتا progress (`/api/v1/progress`). الشرح يأتي مع إجابة الـquiz
- [ ] **apple client id** — iOS فلازم `APPLE_CLIENT_IDS` مضبوط على السيرفر قبل إطلاق الشاشة
- [ ] **إغلاق الحساب** — موجود (`DELETE /me`) لكن بدون مهلة سماح ولا تأكيد
- [ ] **placement test** — غير موجود، المستوى يبدأ `A1`

---

## 13. أخطاء شائعة

| العَرَض | السبب |
| --- | --- |
| `422` على أول صفحة | ترسل `cursor=` فارغاً — احذفه (§7.2) |
| `AUTH_REFRESH_REUSED` بلا سبب | سباق refresh — أضف single-flight (§5.5) |
| طرد المستخدم من كل الأجهزة | السبب نفسه: سباق refresh (§5.5) |
| لا يعمل الكاش في webview | `ETag` غير-exposed (§7.7) |
| `422` عند تعديل الملف | ترسل `currentLevel` (§7.1) |
| `401` متكرر بعد 15 دقيقة | لا يوجد refresh تلقائي |
| `429` عند الدخول | الحد 10/دقيقة لكل IP — عادي، لا تعِد فوراً |
| ضاع `limit` بعد تحويل 307 | عميلك لا يرسل query عند التحويل (§7.4) |
| شكل الخطأ لا يطابق OpenAPI | كان `422` يُنشر بشكل خاطئ — أُصلح (§6)، و`openapi.json` الآن مطابق لما يُرسَل فعلاً |
| `AUTH_IDENTITY_CONFLICT` بعد دخول بمزوّد ثانٍ | المزوّد لم يؤكّد البريد — استخدم المزوّد الأصلي، أو اطلب من المستخدم تأكيد البريد ثم أعد المحاولة |
| تصميم شاشة "تسجيل" منفصلة | لا معنى لها: أول دخول بـ Google/Apple هو التسجيل (§5.2) |
| تبحث عن الشرح داخل حمولة الدرس أو الكتالوج | الكتالوج لا يكشف الشرح إطلاقًا؛ صار يُعاد مع إجابة الـquiz (§11) |
| `404 QUESTION_NOT_FOUND` لسؤال داخل جلسة | السؤال ضمن درس غير منشور أو غير موجود — لا فرق مقصود (§11) |

---

## 14. أشياء تحتاجها مني

أرسل لي أياً منها:

1. **البيئات** — عنوان staging و production، هل HTTPS مُفعّل، وما هو `CORS_ORIGINS` المطلوب
   لو كان هناك webview.
2. **مثال `device`** — كيف تحصل على FCM token، وهل هو متاح قبل أول دخول؟
3. **قرار quiz** — محرّك المحاولات **مبنيّ** الآن (`POST /api/v1/quiz/sessions` + أمر
   موحَّد على `/quiz/sessions/{id}`)، ويحفظ المحاولات ويصحّح على السيرفر.
4. **قرار التصحيح** — صار الشرح والتصحيح يُعادان مع إجابة الـquiz، لكن
   `correctIndex`/`acceptedAnswers` ما زالا محجوبين على سطح الكتالوج. (والمسار المنفصل
   القديم `/content/questions/{id}/explanation` أُزيل.)

مرجع تقني إضافي: [`client-contract.md`](client-contract.md) (إنجليزي) و
`GET /openapi.json` — يمكنك توليد عميل منه الآن (§6).