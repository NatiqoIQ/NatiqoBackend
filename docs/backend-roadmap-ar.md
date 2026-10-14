# خطة إكمال الـ Backend — من foundations إلى تطبيق كامل

هذه خطة تنفيذية مقسّمة لمراحل. كل مرحلة تُبني فوق ما قبلها، وكل واحدة تنتهي ببوابات
جودة خضراء **ومغطّاة باختبارات**. الهدف النهائي: أن يجد مبرمج الهاتف كل ما يحتاجه
لإطلاق التطبيق بلا أي نقطة-asic Lovelace.

الهدف النهائي بدقة: **أن يجد مبرمج الهاتف كل ما يحتاجه لإطلاق التطبيق، بلا أي
endpoint وهمية.**

---

## 0. الخلاصة التنفيذية

| | الوضع |
| --- | --- |
| البنية التحتية جاهزة | adapters كاملة: Redis, S3, FCM, Stripe, AI, Queue |
| الإعدادات جاهزة | `max_hearts`, `heart_regen_seconds`, `ai_daily_user_budget_usd`, `payment_*`, `signed_url_ttl_seconds` — كلها معرّفة ولم تُستخدم |
| الأخطاء جاهزة | **76 error code** معرّفة، ~20 فقط قابلة للوصول اليوم |
| أكبر اكتشاف | `QuestionModel` فيه `correct_index`, `accepted_answers`, `explanation` — **التصحيح لا يحتاج migration** |
| ما ينقص | 11 جدول اليوم → يحتاج ~22 جدولاً و~50 endpoint |

**القاعدة التي تحكم الخطة:** كل `ErrorCode` معرّف في `app/core/errors.py` هو وعد
بميزة لم تُبنَ بعد. الخطأDead code هو الـspec. الخطط تتبعه.

### مراحل الخطة

| # | المرحلة | يوفّر للتطبيق | جداول جديدة | endpoints | تقدير |
| --- | --- | --- | --- | --- | --- |
| 0 | جاهزية الإنتاج | صيانة لا مزايا | 2 | 5 | 3–4 أيام |
| 1 | Quiz + Progress | **أكبر فجوة اليوم** | 4 | 9 | 8–10 أيام |
| 2 | مراجعة SRS | حلقة_usage يومية | 2 | 4 | 5–6 أيام |
| 3 | Gamification | شاشات التقدّم | 3 | 5 | 5–7 أيام |
| 4 | الاشتراكات | Premium | 3 | 6 | 5–7 أيام |
| 5 | الإشعارات |عودتهم اليومية | 2 | 5 | 4–5 أيام |
| 6 | الوسائط | صور/تسجيل | 1 | 3 | 3 أيام |
| 7 | AI | ميزات ذكية | 1 | 5 | 5–6 أيام |
| 8 | Admin/CMS | كتابة المحتوى | 0 | 12 | 6–8 أيام |
| 9 | مفردات/قواعد | مرجع | 2 | 4 | 3 أيام |
| 10 | قرارات مفتوحة | — | — | — | — |

**الإجمالي: ~50 يوم عمل، 24 جدولاً، ~53 endpoint.**

المراحل 0→1→2 هي **الحرجة**. التطبيق لا يعمل بدون 1.

---

## المرحلة 0 — جاهزية الإنتاج (لا مزايا، لكن أنهيار بسببها)

**لماذا أولاً:** هذه ليست ميزة، هي فشل تشغيلي مؤجّل.

**تحديث مهم — البريد خرج من الصورة.** كان أكبر بند هنا هو البريد (استعادة كلمة المرور
وتأكيد العنوان). لم يبق منه شيء: الدخول الآن عبر **Google و Apple** فقط، والسيرفر لا
يخزّن كلمة مرور ولا يرسل بريداً. حُذفت `password_reset_tokens` و
`email_verification_tokens`، وحُذفت معها `PasswordResetDeliveryPort` و`delivery.py`
و`email.py` و endpoints الـ`verify-email` و`change-password`. أي بند بريدي كان في هذه
المرحلة **ملغى**، و`email_verified_at` يُضبط الآن من تأكيد المزوّد.

### المطلوب

| # | المطلوب | التفاصيل |
| --- | --- | --- |
| 0.1 | ~~`SmtpPasswordResetDelivery`~~ | **ملغى** — لا بريد في النظام |
| 0.2 | ~~جدول `email_verification_tokens`~~ | **ملغى** — الحقل يُضبط من المزوّد |
| 0.3 | ~~`POST /api/v1/auth/verify-email`~~ | **ملغى** |
| 0.4 | ~~`POST /api/v1/auth/resend-verification`~~ | **ملغى** |
| 0.5 | ~~`POST /api/v1/auth/change-password`~~ | **ملغى** — لا كلمة مرور تُغيّر |
| 0.6 | ~~`DELETE /api/v1/me`~~ | **تم** — مُنفّذ، ويُبطل كل الجلسات |
| 0.7 | ضبط `GOOGLE_CLIENT_IDS` / `APPLE_CLIENT_IDS` | بدونها يُرفض الدخول كلياً: مزوّد بلا client ids يُرجع `401 AUTH_PROVIDER_UNAVAILABLE`، وتوكن لعميل غير مُدرَج يُرفض |
| 0.8 | ~~`Idempotency-Key` middleware~~ | **تم** — مُنفّذ ومُختبَر |
| 0.9 | `GET /api/v1/me/export` | تصدير كل بياناته. مطلوب لـ GDPR. |
| 0.10 | تفعيل OTEL + Sentry | `otel_enabled` و `sentry_dsn` معرّفان، غير مستخدمين |
| 0.11 | **تشغيل البوابات على PostgreSQL حقيقي** | كل الاختبارات تعمل على SQLite. هذا أخطر فجوة |

### 0.11 هو الأهم

الـschema مُختبَر على SQLite فقط. Postgres يُظهر أشياء لا يفعلها SQLite: `JSONB` مقابل
`JSON`، `TIMESTAMPTZ`، حصر `UUID`، وسلوك `ON CONFLICT`. أي واحد منها يظهر بعد النشر.

```bash
# يجب أن يصبح جزءاً من البوابات
$env:DATABASE_URL="postgresql+asyncpg://natiqo:***@localhost:5555/natiqo"
uv run alembic upgrade head
uv run pytest -q          # يجب أن يمر كاملة على Postgres
```

---

## المرحلة 1 — Quiz + Progress (المرحلة الحاسمة)

هذه المرحلة تحوّل التطبيق من "قارئ" إلى "تطبيق". **لا يمكن اختصارها أو تأجيلها.**

### القرار المعماري الأول

> **التصحيح يتم على السيرفر، دائماً. مفتاح الإجابة لا يترك السيرفر أبداً.**

هذا غير قابل للتفاوض لثلاثة أسباب:

1. **مفتاح الإجابة في قاعدة البيانات أصلاً.** `correct_index` و `accepted_answers` و
   `explanation` موجودة في `QuestionModel`. لا تحتاج migration. فقط تحتاج route محمي.
2. **العميل غير موثوق.** لو أرسلت `correctIndex` للعميل، أي شخص يفتح Charles/Proxyman
   يقرأ الإجابات. كل مستخدم يحصل على A1 مجاناً.
3. **هذا يجعل `points` و`hearts` منطقية.** لا يمكن إنقاص قلب من رقم يرسله العميل.

### decisionة فرعية مطلوبة قبل الكود

**ال.Unlock كيف يعمل؟** الجداول اليوم بلا أي عمود unlock. ثلاثة خيارات:

| الخيار | السلوك | تعقيد |
| --- | --- | --- |
| **A. تسلسلي** | الدرس التالي يُفتح بإتمام السابق | ⭐ أبسط — الـUI وحده يكفي |
| **B. متطلب صريح** | عمود `prerequisite_lesson_id` | ⭐⭐ مرن، يحتاج graph traversal |
| **C. Level gate** | المستوى كاملاً يفتح دفعة | ⭐⭐ متوسط |

**توصيتي: A.** ثم B إن احتجت اجتيازاً. لا تبدأ بـ C — تعقيد بلا فائدة في أول 100 درس.

### الجداول

```python
quiz_sessions(
    id, user_id, mode,            # 'lesson' | 'review' | 'placement'
    lesson_id,                    # nullable (placement/review بلا درس)
    status,                       # 'active'|'completed'|'expired'|'abandoned'
    question_ids JSONB,           # الترتيب المجمّد عند البدء — لا تتغيّر
    started_at, expires_at, completed_at,
    hearts_at_start, correct_count, total_count, xp_awarded
)

quiz_answers(
    id, session_id, question_id,
    position, given_answer JSONB,   # {choice:'x'} | {text:'...'} | {boolean:true}
    is_correct, points_awarded, explanation,
    answered_at,
    UNIQUE(session_id, question_id)   # يمنع إعادة الإجابة
)

user_stats(                       # Phase 3 يوسّعه، نبدأه الآن بـ XP وstreak
    user_id PK, xp_total, current_streak, longest_streak,
    last_active_date, hearts, hearts_updated_at, timezone
)

lesson_progress(
    id, user_id, lesson_id, state, # 'locked'|'available'|'in_progress'|'completed'
    best_score, attempts, xp_earned,
    completed_at,
    UNIQUE(user_id, lesson_id)
)
```

### الـEndpoints

| Method | Path |_jobs | ملاحظات |
| --- | --- | --- | --- |
| `POST` | `/api/v1/quiz/sessions` | ابدأ درس | يعيد sessionId + الأسئلة **بلا** مفتاح + `hearts` |
| `GET` | `/api/v1/quiz/sessions/{id}` | استئناف | **ضروري للموبايل**: لو أُغلق التطبيق midstrip |
| `POST` | `/api/v1/quiz/sessions/{id}/answers` | إجابة واحدة | يُصحّح، يعيد `correct` + `explanation` |
| `POST` | `/api/v1/quiz/sessions/{id}/complete` | إنهاء | XP، فتح الدرس التالي، تحديث الـstreak |
| `POST` | `/api/v1/quiz/sessions/{id}/abandon` | إلغاء | يعيد الـhearts |
| `GET` | `/api/v1/progress/summary` | شاشة البداية | streak، xp، دقة، hearts، review due |
| `GET` | `/api/v1/progress/lessons/{id}` | حالة درس | locked/available/completed |
| `GET` | `/api/v1/progress/unlocks` | ما يفتح بعد ماذا | للـUI التخطيطي |
| `GET` | `/api/v1/progress/xp` | سجل XP | لشاشة السجل فقط |

### الطلب/الاستجابة

```jsonc
// POST /quiz/sessions
{ "mode": "lesson", "lessonId": "uuid" }

// 201
{
  "session": { "id":"uuid", "mode":"lesson", "lessonId":"uuid", "status":"active",
               "expiresAt":"...", "questionCount":6, "answeredCount":0,
               "hearts":5, "maxHearts":5 },
  "questions": [ { "id":"uuid", "position":0, "kind":"multiple_choice",
                   "skill":"vocabulary", "prompt":"...", "points":1,
                   "choices":["a","b","c"] } ]     // ← correct_index غير موجود هنا
}

// POST /quiz/sessions/{id}/answers
{ "questionId":"uuid", "answer": {"choice":"b"} }

// 200
{ "isCorrect":true, "correctAnswer":{"choice":"c"},   // فقط بعد الإجابة
  "explanation":"...", "pointsAwarded":1,
  "sessionHearts":5, "answeredCount":1, "totalCount":6,
  "isComplete":false }
```

**`correctAnswer` يظهر بعد الإجابة فقط** — لا قبلها. هذا هو العقد.

### الأخطاء المستخدمة

`QUIZ_SESSION_NOT_FOUND` · `QUIZ_SESSION_EXPIRED` · `QUIZ_ALREADY_COMPLETED` ·
`QUIZ_NOT_COMPLETED` · `QUESTION_NOT_FOUND` · `QUESTION_NOT_IN_SESSION` ·
`QUESTION_ALREADY_ANSWERED` · `QUESTION_TYPE_UNSUPPORTED` · `INSUFFICIENT_HEARTS` ·
`PROGRESS_NOT_FOUND` · `LESSON_LOCKED` · `LESSON_NOT_AVAILABLE` ·
`LESSON_ALREADY_COMPLETED`

### تصميم النقاط — منفّذ جزئياً

```
correct من أول محاولة   → نقاط كاملة      ← منفّذ
خطأ أول ثم صحيح        → نصف نقاط (÷2)   ← منفّذ
خطأ ثم خطأ             → لا نقاط، ينقص قلب ← القلوب خارج النطاق حالياً
```
مصمم كـ"first try = full". المنطق كله في `quiz/domain/policy.py` عبر دالة
`points_awarded` واحدة، والقسمة على اثنين مع التقريب للأسفل صريحة: سؤال بنقطة واحدة
لا يمنح شيئاً عند إعادة المحاولة. الإجابة الثانية تُعاد كتابتها على نفس صف
`quiz_answers` (لا صف جديد)، فيبقى `UNIQUE (session_id, question_id)` صالحاً ويُحسب
السؤال مرة واحدة. ولا يُعاد فتح سؤال أُجيب صحيحاً: `QUESTION_ALREADY_ANSWERED`.

القلوب (`sessionHearts` / `INSUFFICIENT_HEARTS`) لا تزال خارج النطاق: لا عمود في
`quiz_sessions` ولا خصم عند الخطأ، وقرارك مطلوب قبل تنفيذها.

### كما يجب أن تكون الـidempotency

`POST /quiz/sessions` بـ`Idempotency-Key` — لو ضاعت استجابة الشبكة، العميل يعيد
المحاولة بنفس المفتاح ويعيد نفس `sessionId`. بدونه ينشئ التطبيق جلسات مزدوجة وقلوباً
مخصومة بلا سبب.

---

## المرحلة 2 — مراجعة SRS (المسافة بين الجلسات)

**لماذا هي المرحلة 2 لا 3:** بدونها، `REVIEW_NOTHING_DUE` بلا معنى، وحلقة "افتح
التطبيق يومياً" — وهي هدف المنتج — غير موجودة.

### الخوارزمية: SM-2

معيارacapacity. كل بطاقة: `ease_factor`, `interval_days`, `repetitions`, `due_at`.

```
quality (0–5) حسب، من إجابة صحيحة من أول محاولة → 5، من أول خطأ → 2
repetitions=0 → interval=1 يوم، 1 → 6 أيام، ≥2 → interval×ease
quality < 3  → repetitions=0، interval=1، due_at=اليوم (إعادة فورية)
ease = max(1.3, ease + 0.1 − (5−quality)×(0.08 + (5−quality)×0.02))
```

### الجداول

```sql
review_cards(
    id, user_id, question_id,
    ease_factor, interval_days, repetitions, lapses,
    due_at, last_reviewed_at, state,     -- 'new'|'learning'|'review'|'relearning'
    UNIQUE(user_id, question_id)
)

review_logs(                           -- analytics + spindle
    id, user_id, card_id, quality, previous_interval, new_interval,
    reviewed_at
)
```

### الـEndpoints

| Method | Path | ملاحظات |
| --- | --- | --- |
| `GET` | `/api/v1/review/due?limit=20` | البطاقات المستحقة. `hasMore=false` إذا لا شيء |
| `POST` | `/api/v1/review/cards/{id}/answer` | نفس عقد quiz answer |
| `GET` | `/api/v1/review/stats` | future، due today، retention |
| `GET` | `/api/v1/review/preview` | كم بطاقة متوقعة — للـUI |

**`REVIEW_NOTHING_DUE`** يُستعمل عند `due?limit=` مع `strict=true` والعميل يمنع
"لا يوجد شيء" غامضاً.

**تصميم مهم:** إنشاء البطاقة عند أول إجابة صحيحة في quiz. البطاقات تتولد من كل ما
درسه المستخدم. لا batch night.

---

## المرحلة 3 — Gamification

### خطأ شائع يجب تجنبه: الـjob لحساب الـhearts

```
❌ job كل 30 دقيقة يعيد تعبئة القلوب
✅ حساب كسول عند القراءة: elapsed = now − hearts_updated_at
   refills = elapsed // heart_regen_seconds
   hearts = min(max_hearts, hearts + refills)
```

الطريقة الكسولة بلا job، بلا clock skew،(disposable)، وتعمل بعد توقف السيرفر. الـjob
م.task للـstreak check.

### الجداول

```python
user_stats            # موجود من المرحلة 1 — يوسّع: gems, league_tier, league_updated_at
achievements(         # قالب
    code, title_key, description_key, icon_key, xp_reward, gems_reward,
    condition JSONB,      # {"type":"streak","days":7}
    state, sort_order
)

user_achievements(
    id, user_id, achievement_code, unlocked_at, claimed_at, progress,
    UNIQUE(user_id, achievement_code)
)
```

### الـEndpoints

| Method | Path | ملاحظات |
| --- | --- | --- |
| `GET` | `/api/v1/gamification/summary` | كل شيء في استجابة واحدة — شاشة واحدة في التطبيق |
| `GET` | `/api/v1/achievements` | progress لكل إنجاز |
| `POST` | `/api/v1/achievements/{code}/claim` | `ALREADY_CLAIMED` |
| `GET` | `/api/v1/streak` | الحالي، الأدنى،freeze، shields |
| `POST` | `/api/v1/streak/freeze` | إذا اشتراك مدفوع |

**streamification حساب الإنجازات**: تحقّق شرط عند كل نقطة إنجاز (quiz complete, review
complete, streak change). موظّف (event bus) لا request، وإلا كل إجابة تفحص 40 صفاً.

**`PREMIUM_REQUIRED`** — تطبيقه على `POST /streak/freeze` أول استخدام لـ
`AUTH_INSUFFICIENT_ROLE`/role gate. يعطي رابط الشراء.

---

## المرحلة 4 — الاشتراكات والدفع

### المرحلة الأكثر خطر أمنياً

### Decisionة: هل الدفع له أصلاً في خطتك؟

`currency = "IQD"` في الإعدادات، و`payment_provider` موجود. لكن **الاشتراكات نادرة في
السوق العربي**. قبل أن تبني 6 endpoints وتدمج Stripe:

**إذا لم تكن لديك حسابات Stripe مدفوعة في Iraq — لا تبنِ.** بدّل premium بـ:
- حد يومي أكبر (بدل قلوب لا نهائية)
- بدون إعلانات
- إحصائيات متقدمة

هذا 5% من العمل ونفس القيمة المدركة. **قرار مطلوب منك قبل المرحلة 4.**

### إذا قررت الذهاب للـ Premium

| Method | Path | auth | ملاحظات |
| --- | --- | --- | --- |
| `GET` | `/api/v1/plans` | — | قائمة الأسعار |
| `GET` | `/api/v1/subscription` | Bearer | الحالة الحالية |
| `POST` | `/api/v1/subscription/checkout` | Bearer | ينشئ Stripe Checkout Session |
| `POST` | `/api/v1/subscription/cancel` | Bearer | |
| `POST` | `/api/v1/webhooks/stripe` | **توقيع** | بلا auth |
| `GET` | `/api/v1/subscription/portal` | Bearer | Stripe Customer Portal |

### أمن الـWebhook — غير قابل للتفاوض

```python
# 1. تحقّق التوقيع على Raw body (لا JSON بعد التحليل!)
#    stripe.Webhook.construct_event(raw_body, sig_header, settings.payment_webhook_secret)
# 2. idempotency: event_id فريد — INSERT-first
#    Stripe يعيد إرسال كل event عدة مرات
# 3. لا تثق بأي شيء من body قبل التحقق — ولا من event.type
```

**`payment_events.id` UNIQUE** هو ما يمنع تحصيل الفاتورة مرتين عند إعادة التشغيل.
اختبار إلزامي: أرسل نفس webhook 10 مرات → صفر تحصيل مزدوج.

`PAYPMENT_INVALID_SIGNATURE` → 400. `PAYMENT_EVENT_ALREADY_PROCESSED` → 200 مع
`duplicate: true` (**لا** ترجع خطأ — Stripe يعيد المحاولة عند 4xx).

---

## المرحلة 5 — الإشعارات

### الجداول

```python
notifications(
    id, user_id, kind,        # 'daily_reminder'|'streak_warning'|'review_due'|...
    title_key, body_key,      # مفاتيح ترجمة، لا نصوص — التطبيق يترجم
    data JSONB, read_at, created_at
)

notification_prefs  # موجودة داخل settings؛ نُخرجها لجدول مستقل عند الحاجة
```

**مفتاح الترجمة لا النص**: `title_key: "reminder.streak.title"` — التطبيق يعرض بلغته.
عربي وإنجليزي من ترجمة، بلا نشر جديد.

### الـEndpoints

| Method | Path | ملاحظات |
| --- | --- | --- |
| `GET` | `/api/v1/notifications?cursor=` | صفحة |
| `POST` | `/api/v1/notifications/{id}/read` | |
| `POST` | `/api/v1/notifications/read-all` | |
| `DELETE` | `/api/v1/notifications/{id}` | |
| `GET` | `/api/v1/notifications/unread-count` | للـbadge |

### المجدولة

`queue` port موجود (Redis/memory)، `fcm` port موجود.

```
job: daily_reminder  → 20:00 بتوقيت المستخدم (من settings.timezone)
job: streak_warning  → قبل الغروب إن لم يتعلم اليوم
job: review_due      → عدد البطاقات المستحقة > 0
job: weekly_report   →igmoid
```

**كل رسالة `notification_preferences` قبل الإرسال.** `NOTIFICATION_DISABLED`.

`quietHoursStart/End` (22→8) موجودة في settings — يحترمها الـjob.

---

## المرحلة 6 — الوسائط

`storage` port موجود (local + S3)، `signed_url_ttl_seconds = 900` معرّف. الناقص route
فقط.

| Method | Path | التدفق |
| --- | --- | --- |
| `POST` | `/api/v1/media/presign` | يعيد URL موقّع ١٥ دقيقة + `mediaKey` |
| `POST` | `/api/v1/media/confirm` | يتحقق أن الملف موجود في S3، ينشئ row |
| `GET` | `/api/v1/media/{id}` | يعيد signed URL للقراءة |

```
1. POST /media/presign  { kind:"avatar", contentType:"image/jpeg" }
2. العميل PUT مباشرة إلى S3   ← الـbackend لا يستقبل البايتات
3. POST /media/confirm { mediaKey }
4. server يقيس الحجم والنوع ويقرأ أول bytes للتحقق من magic number
```

**فائدة كبيرة:** `max_request_body_bytes` (1MB) لن يضغط؛ الفيديو يذهب مباشرة إلى S3.
Faster على شبكة الجوال.

`MEDIA_TYPE_NOT_ALLOWED` · `MEDIA_UPLOAD_FAILED` · `MEDIA_NOT_FOUND`

**مهم:** تحقّق **sniffing نوع الملف الحقيقي**، لا `Content-Type` المُعلن. يرسل
المستخدم `.jpg` وهو bash script.

---

## المرحلة 7 — ميزات AI

الوحدة موجودة (`usage`, `budget`, `repositories`, `providers` OpenAI + Anthropic)،
`ai_enabled=false` افتراضياً، ولا route.

### Decisionة: ما الذي يستحق AI فعلاً؟

| الميزة | القيمة | التكلفة | الأولوية |
| --- | --- | --- | --- |
| شرح الإجابة الخاطئة | عالية | منخفضة (short) | ⭐⭐⭐ |
| توليد تمرين من درس | عالية | متوسطة | ⭐⭐⭐ |
| محادثة حرة (AI tutor) | عالية جداً | عالية | ⭐⭐ |
| تصحيح النطق | متوسطة | عالية | ⭐ (بعد ElevenLabs) |

### نمط التنفيذ: **متزامن للشرح، غير متزامن للتوليد**

```
شرح الإجابة     → POST مباشر، 2-4s، experience فوري
توليد تمرين     → POST → jobId → GET /jobs/{id}  (قد يtake 30s)
```

**لا ترسل طلب AI متزامن قد ينتهي بناءً على مهلة 20 ثانية.** عميل جوال على شبكة ضعيفة
سيصل مهلة، ويتكرر الطلب، فتنكشف التكلفة مرتين.

| Method | Path | ملاحظات |
| --- | --- | --- |
| `POST` | `/api/v1/ai/explain` | متزامن، قصير |
| `POST` | `/api/v1/ai/drafts` | → `jobId`، للمراجعة |
| `GET` | `/api/v1/jobs/{id}` | `JOB_NOT_FOUND` · `JOB_FAILED` |
| `POST` | `/api/v1/ai/pronunciation` | بعد المرحلة 12 |
| `GET` | `/api/v1/ai/quota` | لم يبق كم |

### الميزانية — موجودة، استخدمها

`ai_daily_user_budget_usd = 0.50` + `AiUsageRecord`. `AI_QUOTA_EXCEEDED` يُطبَّق
قبل الطلب لا بعده. **ليس بعده** — بعده تعني أنك دفعت.

`AI_UNSAFE_INPUT` + `ai_max_input_chars=4000` — تحقق قبل الإرسال، لا عند المزوّد.

---

## المرحلة 8 — Admin / كتابة المحتوى

**بلا هذا، كل محتوى جديد يحتاج migration.** 100 درس جديد = 100 ملف seed + deploy.

| Method | Path | ملاحظات |
| --- | --- | --- |
| `POST` `GET` `PATCH` `DELETE` | `/api/v1/admin/content/levels` | |
| `POST` `GET` `PATCH` `DELETE` | `/api/v1/admin/content/units` | |
| `POST` `GET` `PATCH` `DELETE` | `/api/v1/admin/content/lessons` | |
| `POST` `GET` `PATCH` `DELETE` | `/api/v1/admin/content/questions` | |
| `POST` | `/api/v1/admin/content/lessons/{id}/publish` | `INVALID_STATE_TRANSITION` |
| `POST` | `/api/v1/admin/content/lessons/{id}/questions/reorder` | bulk |
| `GET` `POST` | `/api/v1/admin/users` | دور `admin` |

**حماية مزدوجة:** `AUTH_INSUFFICIENT_ROLE` (403) + **كل كتابة في `audit_log`**.
من يقدر ينشر درساً يجب أن يُسجَّل ذلك.

### state machine — موجودة

`assert_transition_allowed(current, target)` في `content/domain/policy.py` جاهزة:

```
draft → published → archived
         ↓
      archived → published   (إعادة نشر)
```

`question_shape_problems()` جاهزةSSxSS لفحص صحة السؤال قبل النشر.

**مهم:** انشر وتحقق في الـadmin أن السؤال لا يمكن نشره بدون `correct_index`. خلاف
ذلك درس مع published ومفتاح ناقص = درس غير قابل for grading.

---

## المرحلة 9 — المفردات والقواعد

`WORD_NOT_FOUND` و `GRAMMAR_TOPIC_NOT_FOUND` معرّفان بلا route. للقراءة فقط.

| Method | Path |
| --- | --- |
| `GET` | `/api/v1/vocabulary?level=A1&page=` |
| `GET` | `/api/v1/vocabulary/{word}` |
| `POST` | `/api/v1/vocabulary/{word}/favorite` |
| `GET` | `/api/v1/grammar/topics` |
| `GET` | `/api/v1/grammar/topics/{id}` |

هذا أخف مرحلة — لا كتابة، فقط قراءة و`user_id` Favorite.

---

## المرحلة 10 — قرارات تحتاج منك (قبل أو أثناء التنفيذ)

| القرار | الخيارات | توصيتي |
| --- | --- | --- |
| **1. هل الدفع مدعوم؟** | Stripe / لا | لا تبنِ. اعمل premium بـgems وحدود |
| **2. قواعد الـUnlock** | A تسلسلي / B prerequisite / C level | A ثم B |
| **3. scoring** | first-try full / always full | first-try full |
| **4.Placement test** | موجود / ابدأ A1 | ابدأ A1. A2 واحد يكفي |
| **5. Social** | صديق / leaderboard | ليس في error codes — قرار منتج |
| **6. حذف حساب** | soft / hard | soft + anonymize (يلزم للـGDPR) |
| **7. SRS algorithm** | SM-2 / FSRS | SM-2. FSRS performer |
| **8. لغات AI** | عربي/إنجليزي | ابدأ إنجليزي — النماذج أفضل |

### قراران أصعب:

**Placement test**: لا يوجد `ErrorCode` له. إمّا تضيفه (مرحلة كاملة)، أو تبدأ كل
المستخدمين A1 ويكملون A2 أولاً. **الثاني يكفي في أول 3 أشهر.**

**Social/leaderboard**: لا أثر في `errors.py`. هذا ليس نقصاً تقنياً بل قرار منتج.
`UserDeviceModel` يخزّن `pushToken` فقط — لا أصدقاء.

---

## ما يجب أن يكون **خارج** الخطة

اكتبها صراحةً لتضيق النطاق:

- **Web/TV/Desktop clients** — الأدوات الحالية للهاتف فقط
- **محادثة AI صوتية** — بعد الـprocurement من ElevenLabs/OpenAI Realtime
- **تعديل ملفات على السيرفر** — الوسائط تمarshaled إلى S3
- **A/B testing** — بعد 10K مستخدم
- **Analytics/telemetry** — SendHandler مع Kiln، أو PostHog في الجوال مباشرة

---

## الترتيب التنفيذي المقترح

```
الأسبوع 1     مرحلة 0.5 (Postgres) — قبل أي شيء آخر
الأسبوع 1–3   مرحلة 1 (Quiz + Progress)
الأسبوع 3     مرحلة 0.1–0.8 (parallel — لا يعتمد على 1)
الأسبوع 4–5   مرحلة 2 (SRS)
الأسبوع 5–7   مرحلة 3 (Gamification)
الأسبوع 7+    مرحلة 8 (Admin) — بالتوازي مع 4، تحرر النشر
بعد 1.2       مرحلة 4 (الدفع) — فقط إذا قررت ذلك
```

### بوابة كل مرحلة

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q --cov=app          # ≥ 80%
uv run alembic check               # على Postgres
```

**وإلزامي** (`test_every_published_operation_is_covered`):
كل endpoint جديد بلا اختبار يفشل البناء. لا استثناء.

---

## تعريف الاكتمال

التطبيق جاهز عندما يستطيع مبرمج الهاتف:

- [ ] يشوف شاشة البداية (streak + hearts + due count) من طلب واحد
- [ ] يكمّل درساً، يُصحَّح كل سؤال، تنزل القلوب
- [ ] لو أغلق التطبيق midstrip، يستأنف(session resume)
- [ ]gets error مع `explanation`
- [ ] sees الدروس المقفلة/unlocked
- [ ] يجيب على مراجعة SRS
- [ ] يحصد إنجازاً
- [ ] يستلم إشعار تذكير
- [ ] يرفع صورة شخصية بـ presign
- [ ] يولّد عميلاً من `/openapi.json` ويصير كل error shape صحيح

---

## ملخص: الأدلة

| الاستنتاج | الدليل |
| --- | --- |
| المنتج Duolingo-like | `max_hearts`, `heart_regen_seconds`, `premium_unlimited_hearts`, `INSUFFICIENT_HEARTS` |
| الـQuiz لم يُبنَ | 10 error codes quiz بلا route |
| التصحيح لا يحتاج migration | `correct_index`, `accepted_answers`, `explanation` في `QuestionModel` |
| الـAI جاهز ينتظر route | `platform/ai/providers.py` + `AiUsageRecord` + `ai_*` settings |
| الدفع لم يُبنَ | `payment_provider`, `currency=IQD`, 5 payment error codes |
| البريد خارج النظام | لا كلمة مرور ولا بريد: الدخول بـ ID token من Google/Apple، و`delivery.py` و`email.py` حُذفا |
| Postgres لم يُختبر | كل الاختبارات SQLite |
|Observability معطّلة | `otel_enabled`, `sentry_dsn` غير مستخدمين |