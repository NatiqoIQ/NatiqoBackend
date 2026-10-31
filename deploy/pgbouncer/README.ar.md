# PgBouncer — لماذا، وكيف يُشغَّل، وكيف يُتحقق منه

الملف `docker-compose.pooled.yml` يضيف طبقة PgBouncer اختيارية. الهدف ليس "أفضل ممارسة"
بل رقم محدد: **مئات آلاف المستخدمين لا تعني مئات آلاف اتصالات PostgreSQL**. كل اتصال
خادم PostgreSQL يكلّف ذاكرة وعملية fork؛ `max_connections=500` هو سقف واقعي، بينما
التطبيق يحتاج آلاف العملاء المتزامنين.

## التشغيل

```bash
docker compose -f docker-compose.yml -f docker-compose.pooled.yml --profile pooled up -d
```

- `app` و`worker` يستخدمان `pgbouncer:6432`.
- `migrate` يبقى على `db:5432` **عمداً**: الترحيلات و`pg_advisory_xact_lock` لا يجوز أن
  تمرّ عبر transaction pooling.
- PgBouncer لا ينشر منفذاً إلى المضيف؛ سطح الوصول يبقى API:8000 فقط.

## حساب الأحجام

القاعدة:

```
DATABASE_POOL_SIZE (لكل نسخة)  <=  DEFAULT_POOL_SIZE / عدد نسخ app+worker
DEFAULT_POOL_SIZE × عدد النسخ  <  max_connections - 20 (صيانة/نسخ احتياطي)
MAX_CLIENT_CONN                >=  (pool_size + max_overflow) × عدد النسخ
```

مثال لـ3 نسخ API + 2 worker و`max_connections=200`:

| المتغير | القيمة | لماذا |
|---|---|---|
| `DEFAULT_POOL_SIZE` | 30 | 30 × 5 = 150 < 180 |
| `DATABASE_POOL_SIZE` | 6 | 6 × 5 = 30 لكل نسخة، أقل من 30 |
| `DATABASE_MAX_OVERFLOW` | 2 | ذروة قصيرة لا تتحول إلى ضغط دائم |
| `MAX_CLIENT_CONN` | 2000 | يتسع لكل عملاء النسخ |

في transaction mode لا تعتمد على:
- `SET SESSION` أو مؤقتات جلسة (تُعاد إلى خادم آخر في المعاملة التالية)؛
- prepared statements على مستوى الجلسة (استخدم `statement_cache_size=0` أو
  `prepare_threshold=None` إن ظهرت أخطاء `prepared statement already exists`)؛
- `LISTEN/NOTIFY` أو advisory locks على مستوى الجلسة.

التطبيق يستخدم `SET LOCAL`/`statement_timeout` عبر `server_settings`، وهي معاملات بدء
جلسة يمرّرها asyncpg. إن رفضها PgBouncer، أضفها إلى `IGNORE_STARTUP_PARAMETERS` واضبط
المهلات على مستوى PostgreSQL أو PgBouncer بدلاً منها.

## التحقق (يُشغَّل في staging قبل الإنتاج)

```bash
# 1) الاتصال يعمل من داخل الشبكة
docker compose -f docker-compose.yml -f docker-compose.pooled.yml exec app \
  python -c "import asyncio,asyncpg; asyncio.run(asyncpg.connect('postgresql://natiqo:...@pgbouncer:6432/natiqo'))"

# 2) لا تسريب اتصالات: العدد الثابت تقريباً تحت الحمل
docker compose exec db psql -U natiqo -d natiqo -c \
  "select count(*) from pg_stat_activity where application_name='natiqo-api';"

# 3) لوحة PgBouncer
docker compose -f docker-compose.yml -f docker-compose.pooled.yml exec pgbouncer \
  psql -h 127.0.0.1 -p 6432 -U natiqo pgbouncer -c "SHOW POOLS;"
```

معيار النجاح: عدد اتصالات PostgreSQL يبقى قريباً من `DEFAULT_POOL_SIZE × النسخ` مهما
ارتفع RPS، بينما `cl_waiting` في `SHOW POOLS` يعود إلى صفر بعد الذروة.

## التراجع

```bash
docker compose -f docker-compose.yml up -d   # بلا ملف pooled
```

لا حاجة لتغيير `.env`: `DATABASE_URL` الأصلي ما زال يشير إلى `db`.
