# Runbook: تمرين تجاوز الفشل (P2-6)

هدف هذا الملف ليس «كيف نُشغّل نسخة احتياطية» — ذلك في
[backup-restore-ar.md](backup-restore-ar.md) — بل **كيف نثبت أن النظام يعود للعمل عندما
تموت قاعدة البيانات الأساسية**، وبأي زمن. حساب التطبيق صار يقرأ الكتالوج ولوحة الإدارة من
نسخة القراءة (`DATABASE_READ_URL`، انظر P2-1)، لكن الكتابة كلها على الأساس؛ لذلك ترقية
النسخة هي قرار التشغيل الأول عند الفشل.

## 1) الأهداف

| المقياس | الهدف | كيف يُثبت |
|---|---|---|
| RTO لتجاوز فشل القاعدة | ≤ 15 دقيقة | تمرين ربع سنوي موقوت |
| RPO | ≤ 5 دقائق | PITR + تأكيد `latest_restorable_time` |
| زمن كشف الفشل | ≤ 2 دقيقة | تنبيه `database unreachable` + فشل `/ready` |
| زمن تحويل القراءات للأساس | ≤ 1 دقيقة | إزالة `DATABASE_READ_URL` وإعادة النشر |

## 2) قبل التمرين (مرة واحدة)

1. تأكد من وجود **replica متزامنة** (أو async مع lag مقاس)، وأن لها عنوان DNS مستقر
   (`natiqo-replica.internal`) لا عنوان IP.
2. تأكد أن `DATABASE_READ_URL` في الإنتاج يشير إلى نفس العنوان، وأن `DATABASE_URL`
   يشير إلى الأساس عبر عنوان منفصل (`natiqo-primary.internal`).
3. تأكد أن سر الاتصال موجود في Secret Manager، وأن تدوير الأسرار لا يتطلب لمس الصور.
4. سجّل خط أساس: `pg_stat_replication.replay_lag`، وعدد الاتصالات، و`/ready` من كل نسخة.

## 3) خطوات التجاوز (Failover)

> نفّذها من جهاز تشغيل واحد، وسجّل الوقت عند كل خطوة. في تمرين staging استخدم أسماء
> مختلفة؛ لا تجرّب على الإنتاج.

1. **أوقف الكتابة أولاً** — أنزل الـAPI إلى 0 نسخ أو اجعل الـService يشير إلى نسخة
   صيانة تعيد 503. الهدف: لا كتابة تصل للأساس أثناء الترقية.
   ```bash
   kubectl scale deployment/natiqo-api --replicas=0
   ```
2. **أكّد أن الأساس ميت فعلاً** لا مجرد انقطاع شبكة بين الـAPI والقاعدة:
   ```bash
   kubectl exec -it deploy/natiqo-api -- python -c "import socket; socket.create_connection(('natiqo-primary.internal',5432),3)"
   ```
   إن نجح الاتصال فالمشكلة في التطبيق/الأسرار، لا في القاعدة — أوقف التمرين هنا.
3. **رقِّ النسخة** (الأمر يختلف حسب المزوّد):
   ```bash
   # AWS RDS
   aws rds promote-read-replica --db-instance-identifier natiqo-replica
   # GCP Cloud SQL
   gcloud sql instances promote-replica natiqo-replica
   ```
   انتظر حتى تصبح الحالة `available` وقابلة للكتابة.
4. **أعد توجيه العنوان**: اجعل `natiqo-primary.internal` يشير إلى النسخة المُرقّاة
   (CNAME أو Private Hosted Zone). لا تغيّر `DATABASE_URL` في السر إن كان العنوان ثابتاً —
   هذا سبب وجوده.
5. **أزل نسخة القراءة إن كانت هي نفسها**: `DATABASE_READ_URL` يجب أن يعود للأساس
   (أو يُحذف) حتى لا تُقرأ بيانات قديمة من نسخة لم تعد موجودة.
6. **أعد النشر** وانتظر `/ready`:
   ```bash
   kubectl scale deployment/natiqo-api --replicas=3
   kubectl rollout status deployment/natiqo-api --timeout=5m
   curl -fsS https://api.example.com/ready
   ```
7. **تحقق من الكتابة** بطلب حقيقي (ليس `SELECT 1`):
   ```bash
   # تسجيل دخول تجريبي ثم إكمال درس — يجب أن يظهر XP جديد في /progress/summary
   ```
8. **سجّل الزمن**: من كشف الفشل (تنبيه) إلى أول `/ready` ناجح، ومنه إلى أول كتابة ناجحة.

## 4) التحقق بعد التجاوز

- `pg_stat_replication` فارغ أو يشير إلى Replica جديدة (الأساس القديم خرج).
- لا اتصالات معلّقة على العنوان القديم: `SELECT count(*) FROM pg_stat_activity;`
- `pg_stat_database.xact_rollback` لا يرتفع بعد إعادة التشغيل.
- `/ready` أخضر على كل النسخ، و`natiqo_db_pool_waiters` صفر في لوحة Grafana.

## 5) التراجع

إن فشلت الترقية (النسخة لا تصبح قابلة للكتابة)، **لا تحاول إرجاع العنوان للأساس الميت**.
أعد بناء أساس من PITR وفق [backup-restore-ar.md](backup-restore-ar.md)، ثم أعد التوجيه
إليه. ترقية النسخة عملية غير قابلة للعكس في معظم الخدمات المُدارة.

## 6) بعد التمرين

1. أنشئ Replica جديدة من الأساس الجديد وأعد `DATABASE_READ_URL` إليها.
2. حدّث الخطة: هل انخفض RTO؟ هل احتجت خطوة غير مكتوبة؟ أضفها هنا.
3. إن تجاوز RTO الهدف مرتين متتاليتين، فهذه إشارة إلى P2-6 غير مكتمل، لا إلى «التمرين
   كان سيئاً».
