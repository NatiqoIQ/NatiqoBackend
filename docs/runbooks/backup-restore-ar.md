# Runbook: النسخ الاحتياطي والاستعادة (F5)

هذا الملف لا يستبدل خدمة PostgreSQL المُدارة، بل يحدد **ما يجب أن يكون مضبوطاً** و**كيف نثبت أنه يعمل**. النسخ الاحتياطي غير المُختبَر ليس نسخاً احتياطياً.

## 1) الأهداف

| المقياس | الهدف | كيف يُثبت |
|---|---|---|
| RPO (أقصى فقدان بيانات) | ≤ 5 دقائق | WAL archiving مستمر / PITR |
| RTO (زمن الاستعادة) | ≤ 60 دقيقة | تمرين استعادة مؤقت ومقاس |
| الاحتفاظ | 7 أيام PITR + 30 يوماً نسخة يومية | سياسة الخدمة |
| نطاق الاستعادة | قاعدة كاملة + كائنات S3 (`media/`) | تمرين منفصل للاثنين |

## 2) الإعداد المطلوب (PostgreSQL مُدار)

على RDS / Cloud SQL / Azure Database:

1. فعّل **automated backups** مع `backup_retention_period >= 7`.
2. فعّل **point-in-time recovery** (WAL archiving). تحقق من `latest_restorable_time` عبر الـ console أو CLI.
3. انسخ النسخ إلى منطقة/حساب مختلف (cross-region / cross-account) — نسخة في نفس الحساب لا تحمي من حذف الحساب.
4. فعّل **deletion protection** على الـ instance، و`--no-delete-protection` لا يُستخدم في الإنتاج.
5. لـ S3: فعّل versioning + lifecycle (نسخة قديمة 30 يوماً) + Object Lock إن كان متاحاً.

تحقق دوري (أسبوعياً) بأمر واحد، حسب المزوّد:

```bash
# AWS RDS
aws rds describe-db-instances \
  --db-instance-identifier natiqo-prod \
  --query 'DBInstances[0].{backup:BackupRetentionPeriod,earliest:LatestRestorableTime,multi:MultiAZ}'

# GCP Cloud SQL
gcloud sql instances describe natiqo-prod \
  --format='value(settings.backupConfiguration.enabled,settings.backupConfiguration.pointInTimeRecoveryEnabled)'
```

## 3) تمرين الاستعادة (شهرياً)

> نفّذه على instance منفصل، أبداً على الإنتاج. سجّل الأوقات في نفس التذكرة.

1. **اختر نقطة زمنية** قبل ساعة من الآن، واكتبها في التذكرة.
2. أنشئ instance جديداً من نسخة/نقطة الاستعادة (`restore-db-instance-from-db-snapshot` أو `gcloud sql backups restore`).
3. وجّه تطبيق staging إلى الـ instance الجديد عبر `DATABASE_URL` (لا تلمس `.env` الإنتاج).
4. شغّل التحقق التالي:

```bash
psql "$STAGING_DATABASE_URL" -c "select count(*) from users;"
psql "$STAGING_DATABASE_URL" -c "select max(created_at) from quiz_sessions;"
psql "$STAGING_DATABASE_URL" -c "select version_num from alembic_version;"

# التطبيق نفسه: هل يقرأ ويكتب؟
curl -fsS "$STAGING_BASE_URL/ready" | jq '.data.status'
```

5. **تحقق من الفرق**: `max(created_at)` في النسخة يجب أن يقع قبل نقطة الاستعادة مباشرة. إن كان أقدم بكثير، فـ RPO غير محقق.
6. سجّل RTO = (وقت جاهزية الـ instance) − (وقت بدء التمرين).
7. أعد تشغيل `alembic upgrade head` على النسخة للتأكد أن الترحيلات متسقة (يجب ألا تفعل شيئاً).
8. احذف instance التمرين بعد أسبوع على الأكثر (لتجنّب التكلفة)، مع الاحتفاظ بالسجل.

## 4) الاستعادة الكاملة (Incident)

1. **أعلن الحادث** وحدد نقطة الاستعادة المطلوبة (آخر لحظة سليمة).
2. أوقف الكتابة: أزل الـ API من الـ LB أو اجعله read-only إن أمكن.
3. استعد إلى instance جديد. **لا تستعد فوق الأصل.**
4. شغّل `alembic upgrade head` وتأكد من `alembic_version`.
5. شغّل فحوصات §3 خطوة 4.
6. بدّل `DATABASE_URL` في secret manager، ثم أعد تشغيل الـ API والـ worker.
7. راقب لمدة 30 دقيقة: `NatiqoHighErrorRate`, `NatiqoDbPoolSaturated`, `NatiqoQueueBacklog`.
8. اكتب Postmortem: كم بيانات فُقدت فعلاً؟ هل RPO/RTO تحققا؟

## 5) ماذا نفعل بالطابور أثناء الاستعادة؟

- أوقف الـ worker أولاً حتى لا يعالج وظائف ضد قاعدة قديمة.
- بعد الاستعادة، شغّل worker واحداً وراقب `natiqo_queue_depth`.
- أي وظيفة نُفّذت بعد نقطة الاستعادة قد تُنفَّذ مرة أخرى (at-least-once). لهذا كل معالجات الطابور يجب أن تكون idempotent.

## 6) معيار الإغلاق لـ F5

- [ ] PITR مفعّل و`latest_restorable_time` يتحدث (لقطة شاشة في التذكرة).
- [ ] نسخة cross-region موجودة.
- [ ] تمرين استعادة واحد مكتمل، RTO مقاس وموثق.
- [ ] Runbook هذا محدّث باسم المزوّد والأوامر الفعلية.
