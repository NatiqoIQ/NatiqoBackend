# تقسيم الجداول الكبيرة (P2-3) — دليل تشغيل

هذا الدليل ينفّذ الجزء الأخير من P2-3: تحويل جدولي السجلّات ذات النمو غير المحدود
(`review_logs` و`ai_usage_records`) إلى جداول مُقسّمة شهرياً في PostgreSQL. التنظيف
الدوري (P2-3) موجود في الكود ويحدّ من الحجم، لكنه لا يحلّ مشكلة **الفهرسة والصيانة**:
فهرس واحد على ملايين الصفوف يتباطأ تدريجياً، و`VACUUM` و`REINDEX` يحتاجان نافذة أطول
كل شهر. التقسيم يجعل كل شهر وحدة مستقلة، ويحوّل الحذف النهائي إلى `DROP PARTITION`
بدل `DELETE` يترك صفوفاً ميتة.

> **النطاق:** لا يغيّر هذا الدليل أي كود تطبيقي. نسخ التطبيق الحالية تكتب في نفس
> الأسماء، والقراءات الحالية لا تبحث بـ`id` مفرد في هذين الجدولين (تحقّقنا: لا يوجد
> `get_by_id` لهما)، لذا إضافة مفتاح التقسيم إلى المفتاح الأساسي آمنة.

## 1) هل نحتاج التقسيم الآن؟

نفّذ الاستعلامين؛ إن كان الجواب أقل من ~5 ملايين صف للجدول الواحد فأجّل العملية،
فالتقسيم يضيف تعقيداً بلا فائدة ملموسة تحت هذا الحد.

```sql
SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS total
FROM pg_stat_user_tables
WHERE relname IN ('review_logs', 'ai_usage_records')
ORDER BY n_live_tup DESC;

-- أقل شهر وأكثره، لتحديد أول وأشهر تقسيم
SELECT date_trunc('month', min(reviewed_at)) FROM review_logs;
SELECT date_trunc('month', max(reviewed_at)) FROM review_logs;
SELECT date_trunc('month', min(created_at)) FROM ai_usage_records;
SELECT date_trunc('month', max(created_at)) FROM ai_usage_records;
```

## 2) قبل النافذة

1. **نسخة احتياطية موثّقة:** `pg_dump -Fc -t review_logs -t ai_usage_records ...`
   ثم استرجاع تجريبي في قاعدة مؤقتة (راجع `backup-restore-ar.md`). لا تبدأ بدون هذا.
2. **أوقف كتابة الـworker** الذي ينظّف الجدولين (`PURGE_REVIEW_LOGS`/`PURGE_AI_USAGE`)
   مؤقتاً. الكتابة من تطبيق الويب تُوقف بوقف حركة الكتابة أو بتفعيل صيانة قصيرة.
3. **قدّر المساحة:** النسخة الجديدة تحتاج مساحة بيانات + فهارس الجدول الأصلي، أي
   ~2.2× حجمه. تحقّق من `df -h` على وحدة التخزين.
4. اختبر الخطوات كاملة على نسخة إنتاج مصغّرة، وسجّل الزمن الفعلي لتحديد النافذة.

## 3) التنفيذ (نافذة صيانة، PostgreSQL ≥ 14)

الطريقة المختارة: إنشاء جدول مُقسّم جديد، نسخ البيانات على دفعات، ثم مبادلة الأسماء.
أبسط من `ATTACH PARTITION` على جدول قائم، وأقل عرضة لقفل طويل، ولا يحتاج `pg_partman`
(يمكن إضافته لاحقاً لأتمتة إنشاء التقسيمات).

### 3.1 `review_logs`

```sql
BEGIN;

-- أعد تسمية الجدول القديم؛ يبقى للقراءة والتحقق حتى النهاية.
ALTER TABLE review_logs RENAME TO review_logs_old;
ALTER TABLE review_logs_old
  DROP CONSTRAINT IF EXISTS pk_review_logs,
  DROP CONSTRAINT IF EXISTS fk_review_logs_card_id_review_cards,
  DROP CONSTRAINT IF EXISTS fk_review_logs_user_id_users,
  DROP CONSTRAINT IF EXISTS ck_review_logs_quality_in_range,
  DROP CONSTRAINT IF EXISTS ck_review_logs_non_negative_new_interval,
  DROP CONSTRAINT IF EXISTS ck_review_logs_non_negative_previous_interval;
-- الأسماء (بما فيها أسماء الفهارس التي تدعم PK) فريدة على مستوى الـschema؛
-- إسقاط قيود الجدول القديم لا يحذف فهارسه، فنحذفها صراحةً قبل إعادة استخدام الأسماء.
DROP INDEX IF EXISTS ix_review_logs_card;
DROP INDEX IF EXISTS ix_review_logs_user;

CREATE TABLE review_logs (
    id uuid NOT NULL,
    card_id uuid NOT NULL,
    user_id uuid NOT NULL,
    quality smallint NOT NULL,
    previous_interval integer NOT NULL,
    new_interval integer NOT NULL,
    reviewed_at timestamptz NOT NULL,
    CONSTRAINT pk_review_logs PRIMARY KEY (id, reviewed_at),
    CONSTRAINT fk_review_logs_card_id_review_cards FOREIGN KEY (card_id)
        REFERENCES review_cards (id) ON DELETE CASCADE,
    CONSTRAINT fk_review_logs_user_id_users FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT ck_review_logs_quality_in_range CHECK (quality BETWEEN 0 AND 5),
    CONSTRAINT ck_review_logs_non_negative_new_interval CHECK (new_interval >= 0),
    CONSTRAINT ck_review_logs_non_negative_previous_interval CHECK (previous_interval >= 0)
) PARTITION BY RANGE (reviewed_at);

-- تقسيم افتراضي يمنع فشل الكتابة إن وصل صف خارج نطاق التقسيمات.
CREATE TABLE review_logs_default PARTITION OF review_logs DEFAULT;
CREATE INDEX ix_review_logs_card ON review_logs (card_id, reviewed_at);
CREATE INDEX ix_review_logs_user ON review_logs (user_id, reviewed_at);

COMMIT;
```

أنشئ تقسيمات الأشهر الموجودة (استخدم نتيجة الاستعلام في الخطوة 1؛ المثال يغطي
2025-01 حتى 2026-12):

```sql
DO $$
DECLARE m date;
BEGIN
  FOR m IN SELECT generate_series('2025-01-01'::date, '2026-12-01'::date, interval '1 month')
  LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS review_logs_%s PARTITION OF review_logs '
      'FOR VALUES FROM (%L) TO (%L)',
      to_char(m, 'YYYY_MM'), m, m + interval '1 month');
  END LOOP;
END $$;
```

انسخ على دفعات (لا `INSERT ... SELECT` واحد؛ يقفل ويملأ WAL):

```sql
-- كرّر حتى يُرجع 0 صفاً؛ 50 ألف صف في الدفعة توازن بين السرعة وضغط WAL.
WITH batch AS (
    SELECT id, card_id, user_id, quality, previous_interval, new_interval, reviewed_at
    FROM review_logs_old
    ORDER BY reviewed_at
    LIMIT 50000
)
INSERT INTO review_logs SELECT * FROM batch
ON CONFLICT DO NOTHING;

DELETE FROM review_logs_old
WHERE id IN (SELECT id FROM review_logs_old ORDER BY reviewed_at LIMIT 50000);
```

> ملاحظة: `DELETE` أعلاه يفترض أن `id` وحده فريد في الجدول القديم، وهو كذلك. نفّذ
> الدفعة داخل معاملة ثم `COMMIT`؛ إن انقطعت العملية أعد التشغيل بأمان بفضل
> `ON CONFLICT DO NOTHING`.

### 3.2 `ai_usage_records`

نفس النمط، بمفتاح `(id, created_at)`:

```sql
BEGIN;
ALTER TABLE ai_usage_records RENAME TO ai_usage_records_old;
ALTER TABLE ai_usage_records_old
  DROP CONSTRAINT IF EXISTS pk_ai_usage_records,
  DROP CONSTRAINT IF EXISTS fk_ai_usage_records_user_id_users;
DROP INDEX IF EXISTS ix_ai_usage_user_created;
DROP INDEX IF EXISTS ix_ai_usage_feature_created;
DROP INDEX IF EXISTS ix_ai_usage_created_at;

CREATE TABLE ai_usage_records (
    id uuid NOT NULL,
    user_id uuid,
    feature varchar(40) NOT NULL,
    provider varchar(40) NOT NULL,
    model varchar(120) NOT NULL,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    latency_ms integer NOT NULL DEFAULT 0,
    cost_usd numeric(12,6) NOT NULL DEFAULT 0,
    status varchar(20) NOT NULL DEFAULT 'ok',
    error varchar(500),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_ai_usage_records PRIMARY KEY (id, created_at),
    CONSTRAINT fk_ai_usage_records_user_id_users FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
) PARTITION BY RANGE (created_at);

CREATE TABLE ai_usage_records_default PARTITION OF ai_usage_records DEFAULT;
CREATE INDEX ix_ai_usage_user_created ON ai_usage_records (user_id, created_at);
CREATE INDEX ix_ai_usage_feature_created ON ai_usage_records (feature, created_at);
CREATE INDEX ix_ai_usage_created_at ON ai_usage_records (created_at);
COMMIT;
```

> **تحقّق من أسماء الأعمدة والقيود من قاعدة الإنتاج قبل التنفيذ:**
> `\d+ ai_usage_records` و`\d+ review_logs`. الأسماء أعلاه مأخوذة من migrations
> المستودع (`2f62b1e13c1c` و`a710e92dc173` وما بعدهما) لكن الإنتاج قد يسبقها.

أنشئ التقسيمات الشهرية ثم انسخ بنفس حلقة الدفعات أعلاه (مفتاح الترتيب
`created_at`).

### 3.3 المبادلة والتحقق

1. تأكد أن عدد الصفوف متساوٍ:
   ```sql
   SELECT (SELECT count(*) FROM review_logs_old) AS old,
          (SELECT count(*) FROM review_logs) AS new;
   -- للأهمية: الفرق يجب أن يكون صفراً. صفوف ذهبت إلى review_logs_default
   -- (تاريخ خارج نطاق التقسيمات) تُحتسب في new، لكنها ليست في التقسيم الشهري.
   SELECT count(*) FROM review_logs_default;
   ```
2. أعِد تشغيل حركة الكتابة (أو ارفع الصيانة)، وراقب `/ready` و`natiqo_db_pool_checked_out`
   و`pg_stat_activity` لدقائق.
3. بعد 24–48 ساعة من التطابق (وبعد نسخة احتياطية جديدة) احذف الجدولين القديمين:
   ```sql
   DROP TABLE review_logs_old;
   DROP TABLE ai_usage_records_old;
   ```

## 4) الصيانة الشهرية

- **إنشاء تقسيم الشهر القادم** قبل حلوله (مهمة cron في أول كل شهر):
  ```sql
  CREATE TABLE review_logs_2027_01 PARTITION OF review_logs
    FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');
  ```
  وجود `review_logs_default` يمنع فشل الكتابة إن نسيت، لكن الصفوف فيه تُفقد ميزة
  التقسيم؛ راقب عدّاده وأفرغه بتقسيم صريح ثم `INSERT ... SELECT` منه.
- **التقاعد:** بدل `DELETE` الملايين:
  ```sql
  DROP TABLE review_logs_2024_01;  -- فوري، بلا WAL ولا bloat
  ```
  طبّق سياسة الاحتفاظ نفسها المعرّفة في الكود (`REVIEW_LOG_RETENTION_DAYS`،
  `AI_USAGE_RETENTION_DAYS`).
- `autovacuum` يعمل لكل تقسيم على حدة؛ لا حاجة لضبط خاص.

## 5) التراجع

- ما دام الجدول القديم موجوداً: أوقف الكتابة أولاً. إن كانت الكتابة قد استُأنفت بعد
  المبادلة، انسخ الصفوف الجديدة فقط إلى القديم قبل التراجع:
  ```sql
  INSERT INTO review_logs_old
  SELECT n.* FROM review_logs n
  WHERE NOT EXISTS (
    SELECT 1 FROM review_logs_old o WHERE o.id = n.id AND o.reviewed_at = n.reviewed_at
  );
  ```
  ثم أعد التسمية وأعد بناء القيود والفهارس التي أُسقطت في 3.1:
  ```sql
  BEGIN;
  ALTER TABLE review_logs RENAME TO review_logs_new;
  ALTER TABLE review_logs_old RENAME TO review_logs;
  ALTER TABLE review_logs
    ADD CONSTRAINT pk_review_logs PRIMARY KEY (id),
    ADD CONSTRAINT fk_review_logs_card_id_review_cards FOREIGN KEY (card_id)
      REFERENCES review_cards (id) ON DELETE CASCADE,
    ADD CONSTRAINT fk_review_logs_user_id_users FOREIGN KEY (user_id)
      REFERENCES users (id) ON DELETE CASCADE,
    ADD CONSTRAINT ck_review_logs_quality_in_range CHECK (quality BETWEEN 0 AND 5),
    ADD CONSTRAINT ck_review_logs_non_negative_new_interval CHECK (new_interval >= 0),
    ADD CONSTRAINT ck_review_logs_non_negative_previous_interval CHECK (previous_interval >= 0);
  CREATE INDEX ix_review_logs_card ON review_logs (card_id, reviewed_at);
  CREATE INDEX ix_review_logs_user ON review_logs (user_id, reviewed_at);
  DROP TABLE review_logs_new;  -- بعد التأكد من النسخة الاحتياطية
  COMMIT;
  ```
  (لـ`ai_usage_records` نفس الخطوات مع أعمدته الثلاثة للفهارس.)
- بعد حذف الجدول القديم لا رجوع إلا من النسخة الاحتياطية؛ لذلك لا تحذفه قبل نافذة
  التحقق.

## 6) ما لا يغطيه هذا الدليل

- `pg_partman` والأتمتة الكاملة (اختياري عند تجاوز 50 مليون صف).
- تقسيم `audit_logs` (سياسة احتفاظ 7 سنوات؛ يُقسَّم سنوياً عند الحاجة).
- تغيير تعريف المفتاح الأساسي في `ReviewLogModel`/`AiUsageRecord` داخل SQLAlchemy.
  التطبيق لا يقرأ هذين الجدولين بالمفتاح المفرد، لكن إن أُضيفت قراءة كهذه مستقبلاً
  يجب استخدام `(id, created_at)` أو `(id, reviewed_at)`.
