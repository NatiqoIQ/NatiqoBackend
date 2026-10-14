# تقرير التدقيق — Natiqo Backend (المرحلة 1)

> نطاق هذا التقرير: مستودع الـBackend فقط (`C:\Users\qut10\Desktop\Natiqo`).
> لا يوجد كود Flutter في هذا المستودع، لذا بنود الواجهة في الخطة (المراحل 7/8) خارج النطاق هنا.
>
> تاريخ التدقيق: 2026-10-14 · الأدوات: ruff 0.16.9 (يضم قواعد bandit `S`)، pyright، pytest 9 + coverage، bandit (فحص مستقل).

## 1. الملخص التنفيذي

| البُعد | النتيجة |
| --- | --- |
| التحليل الثابت (lint) | ✅ `ruff check .` — لا ملاحظات |
| الأنواع (pyright) | ✅ 0 errors / 0 warnings |
| الاختبارات | ✅ 566 ناجح |
| التغطية | ✅ **82%** (الهدف ≥ 80%) |
| فحص الأمان (bandit) | ✅ لا نتائج عالية؛ 1 متوسطة مقبولة موثّقة |
| الأسرار في الكود | ✅ لا أسرار؛ `.env` غير متعقّب |
| CI/CD | ⚠️ كان **بلا بوابة اختبار** — أُصلح في هذا التدقيق |
| بيئة PostgreSQL | ⚠️ غير مُختبرة آليًا (كل الاختبارات على SQLite) |

**الحكم:** قاعدة الكود سليمة ومعماريتها ناضجة (modular monolith + clean architecture).
لا توجد ثغرات حرجة. الفجوتان الأهمّ عمليّتان: **غياب بوابة CI قبل النشر** و**عدم اختبار
PostgreSQL**. كلتاهما عولجت/شُخّصت في هذا التدقيق.

## 2. نطاق التدقيق والمنهجية

تم تنفيذ مهام المرحلة 1 من الخطة على الكود الفعلي:

1. **إعداد بيئة التدقيق** — بدل إنشاء فرع Git مؤقت (كانت هناك تغييرات غير مُودَعة على `main`)، تم التدقيق على الشجرة الحالية دون أي commit. **التوصية:** إنشاء الفرع المؤقت بعد تثبيت التغييرات الحالية.
2. **التحليل الثابت** — `ruff check` + `ruff format --check` + `pyright`. (المستودع يستخدم Ruff/Pyright بدل flake8/pylint/mypy — نفس الغرض، أسرع وأكثر توافقًا مع FastAPI.)
3. **الأسلوب والتوثيق** — فحص README والتعليقات ونمط الكود.
4. **الأمان** — `bandit` مستقل + فحص أنماط الأسرار + مراجعة HTTPS/الرؤوس/تحديد المعدّل وإدارة المفاتيح.
5. **CI/CD والاختبارات** — مراجعة `.github/workflows` وقياس التغطية.

## 3. النتائج مرتّبة بالأولوية

### 🔴 حرجة (Critical)
لا يوجد.

### 🟠 عالية (High)

**H-1 · لا بوابة اختبار قبل النشر — أُصلح**
`deploy.yml` ينشر إلى الإنتاج على **كل** push إلى `main` عبر self-hosted runner، بلا أي lint/type/test قبله. أي commit مكسور يصل الإنتاج مباشرة.
- **الإصلاح المنفَّذ:** أُضيف `.github/workflows/ci.yml` يشغّل `ruff check` + `ruff format --check` + `pyright` + `pytest --cov-fail-under=80` على كل push/PR.
- **متبقٍّ (توصية):** تفعيل Branch Protection على `main` يجعل `CI / quality` فحصًا **إلزاميًا**، وجعل `deploy` يعتمد على نجاح CI (`workflow_run` أو `needs`). الآن الوركتان مستقلتان.

**H-2 · PostgreSQL غير مُختبر آليًا**
كل الاختبارات تعمل على SQLite. Postgres يُظهر فروقًا لا يُظهرها SQLite: `JSONB` مقابل `JSON`، `TIMESTAMPTZ`، حصر `UUID`، وسلوك `ON CONFLICT`. هذه أخطر فجوة تشغيلية (وهي البند 0.11 في `docs/backend-roadmap-ar.md`).
- **التوصية:** إضافة job في CI يشغّل `alembic upgrade head` + `pytest` على خدمة `postgres:16` (service container)، مع `DATABASE_URL=postgresql+asyncpg://...`.
- **شاهد مباشر على الخطورة:** أثناء هذا التدقيق تسبّب **انحراف مخطط SQLite المحلي** (`local.db` بُني من لقطة كود قديمة، ينقصه `users.deleted_at`) في خطأ 503 مُقنّع على `/auth/dev-login`. أُصلح بإعادة بناء الجدول في مكانه. لولا الفجوة نفسها لما حدث.

### 🟡 متوسطة (Medium)

**M-1 · محوّلات الخدمات الخارجية بلا تغطية**
`coverage` يفي بالهدف الإجمالي (82%) لكن طبقة المحوّلات الحقيقية غير مُختبرة:

| الملف | التغطية |
| --- | --- |
| `app/platform/cache/redis.py` | 0% |
| `app/platform/push/fcm.py` | 0% |
| `app/platform/queue/redis.py` | 0% |
| `app/platform/storage/s3.py` | 0% |
| `app/platform/ai/service.py` | 21% |
| `app/platform/ai/providers.py` | 37% |
| `app/platform/identity/jwks.py` | 33% |
| `app/platform/identity/verifiers.py` | 48% |
| `app/modules/ai/infrastructure/usage.py` | 38% |

هذه تُستبدل بـfakes في الاختبارات، فتبقى المسارات الحقيقية غير مُتحقَّق منها. **التوصية:** اختبارات تكامل اختيارية (تُشغَّل عند توفّر الخدمة) لكل محوّل، على غرار ما فُعل في اختبارات PostgreSQL المقترحة.

**M-2 · نمط الكود غير موحّد — أُصلح**
`ruff format --check` كان يفشل على **12 ملفًا** (7 في `app/`، 5 في `tests/`). تم تشغيل `ruff format app tests scripts` فصار كل الملفات منسّقة، وأُدرج `format --check` في CI.
- ملاحظة: `ruff 0.16` ينسّق كتل Python داخل ملفات `.md` أيضًا؛ أُبقي فحص التنسيق مقصورًا على `app tests scripts` تجنّبًا لتعديل الوثائق العربية دون داعٍ.

**M-3 · لا فحص أسرار آلي (secret scanning) في CI**
`.env` غير متعقّب (✅ صحيح، موجود في `.gitignore` مع `!.env.example`)، ولم يُعثر على أسرار مدسوسة في `app/`. لكن لا توجد خطوة `gitleaks`/`trufflehog` في CI. **التوصية:** إضافة `gitleaks` كخطوة في `ci.yml`.

**M-4 · لا بيئة Staging**
النشر يذهب مباشرة من `main` إلى الإنتاج. **التوصية:** بيئة staging منفصلة (فرع `staging` أو compose منفصل) قبل الإنتاج.

**M-5 · الرصد معطّل (Observability)**
`otel_enabled` و`sentry_dsn` مُعرّفان في الإعدادات وغير مستخدمين. لا تتبّع للأخطاء ولا مقاييس في الإنتاج. **التوصية:** تفعيل Sentry على الأقل لالتقاط الاستثناءات (يخدم المرحلة 10).

### 🟢 منخفضة (Low)

**L-1 · `bandit` مستقل:** نتيجة واحدة متوسطة `B104` (bind `0.0.0.0` في `config.py:45`) وهي **مقصودة** وموثّقة (`# noqa: S104`) خلف reverse proxy. بقية النتائج `B105/B106` إنذارات كاذبة (ثوابت أخطاء مثل `AUTH_WEAK_PASSWORD` وأنواع توكن `"access"`/`"refresh"`). لا إجراء.

**L-2 · HTTPS:** يُفرض عبر رأس `Strict-Transport-Security` (`app/core/middleware.py:113`) مع رؤوس `X-Content-Type-Options` و`X-Frame-Options`. لا إعادة توجيه على مستوى التطبيق — يعتمد على TLS عند reverse proxy. **التوصية:** توثيق أن الإنهاء (termination) يحدث في الـproxy، وتفعيل CSP عند بناء الواجهة.

**L-3 · كتل التوثيق العربية** غير مشمولة بفحص التنسيق (قرار مقصود).

## 4. نقاط القوة (يجب الحفاظ عليها)

- **معمارية نظيفة:** فصل `domain` / `application` / `infrastructure` / `presentation`، و`platform/` كطبقة محوّلات، ووحدات `users`/`content`/`quiz`/`ai`.
- **تحقّق إعدادات عند الإقلاع:** `validate_environment()` يرفض الإقلاع الإنتاجي عند: أسرار placeholder، تطابق JWT secrets، طول مفتاح < 32، `DEBUG=true`، غياب Redis، غياب مزوّدي الدخول، تباين AI. كذلك تحقّق CORS مع `*` + credentials.
- **حدّ معدّل مدمج** بنطاقات (`auth`/`catalogue`/`quiz`/`progress`) وحماية من القوة الغاشمة.
- **Idempotency-Key** مُنفَّذ ومُختبَر.
- **أخطاء مُنمّطة** (`ErrorCode`) وحمولات مشكلة عبر مخططات Pydantic.

## 5. معايير القبول للمرحلة 1

| المعيار | الحالة |
| --- | --- |
| إصدار تقرير تدقيق بملاحظات وتحسينات | ✅ هذا التقرير |
| تغطية اختبارات ≥ 80% | ✅ 82% (بوابة `--cov-fail-under=80` في CI) |
| اعتماد سياسة أمان أساسية (HTTPS + إدارة مفاتيح) | ✅ `SECURITY.md` |
| مراجعة CI/CD وضبطها لتشمل البناء والاختبار | ✅ `.github/workflows/ci.yml` |

## 6. خطة الإصلاح المقترحة (بالترتيب)

1. **[High] H-1 المتبقّي:** Branch Protection على `main` + ربط `deploy` بنجاح CI.
2. **[High] H-2:** job PostgreSQL في CI (alembic + pytest على Postgres).
3. **[Medium] M-3:** `gitleaks` في CI. **[Medium] M-5:** تفعيل Sentry.
4. **[Medium] M-1:** اختبارات تكامل للمحوّلات (Redis/S3/FCM/Queue/JWKS).
5. **[Medium] M-4:** بيئة Staging.

---
**المرحلة التالية:** المرحلة 2 (محرك التعلّم) — **منفّذة فعليًا** في هذا المستودع (`modules/quiz` + `modules/content` + `progress`)؛ راجع تحليل الفجوة المرفق مع الرد لمطابقة المراحل 2–11 على الوضع الحالي.
