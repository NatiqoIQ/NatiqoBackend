// k6 mixed-workload test for Natiqo.
//
//   k6 run loadtest/k6/natiqo-mixed.js \
//     -e BASE_URL=https://staging.example.com \
//     -e TOKENS_FILE=$PWD/loadtest/tokens.json
//
// Two ways to authenticate:
//   1. TOKENS_FILE  — a JSON array produced by scripts/mint_loadtest_tokens.py.
//                     Use this for any real capacity test: every virtual user then
//                     has its own account, which is the only way a per-user rate
//                     limit can be exercised honestly.
//   2. DEV_LOGIN    — falls back to POST /auth/dev-login (one shared account).
//                     Fine for a smoke run, wrong for capacity: all VUs share one
//                     user, so the per-user budget (not the server) becomes the
//                     bottleneck. Never enable dev-login in production.
//
// The script deliberately does not assert on answer correctness. It measures the
// server's capacity to grade, persist and complete, not the learner's score.
//
// Environment:
//   BASE_URL              default http://127.0.0.1:8000
//   TOKENS_FILE           optional JSON [{accessToken, refreshToken}, ...]
//   DEV_LOGIN             "true" (default) to use the development account
//   BROWSE_RPS            iterations/s for browse      (default 100)
//   LEARN_RPS             lesson sittings/s for learn  (default 12)
//   PROGRESS_RPS          iterations/s for progress    (default 18)
//
// Note the units: a `learn` iteration is one whole sitting — session start, ten
// answers and a completion, so roughly twelve HTTP requests. The three rates are
// therefore *iterations*, not requests. The defaults above produce roughly 500
// HTTP requests/second; the 2,000 RPS target in the plan is
// BROWSE_RPS=400 LEARN_RPS=50 PROGRESS_RPS=66 (~1,400 reads + ~600 writes).
//   DURATION              default 10m
//   MAX_FAIL_RATE         default 0.005; raise it only for a deliberate chaos run
//   READ_P95_MS           default 200; raise for a chaos drill that pauses Redis
//   READ_P99_MS           default 500
//   WRITE_P95_MS          default 300
//   WRITE_P99_MS          default 800
//   ANSWERS_PER_SESSION   default 0 = answer every question in the sitting
//   THINK_TIME_MS         default 0 = no pause between answers
//   LESSON_IDS            optional comma-separated ids; otherwise the first
//                         published lesson in the catalogue is used
//
// Run it against staging, never against production.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';

const BASE_URL = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const API = `${BASE_URL}/api/v1`;
const TOKENS_FILE = __ENV.TOKENS_FILE || '';
const USE_DEV_LOGIN = (__ENV.DEV_LOGIN || 'true') === 'true';
const ANSWERS_PER_SESSION = Number(__ENV.ANSWERS_PER_SESSION || 0);
const THINK_TIME_MS = Number(__ENV.THINK_TIME_MS || 0);
const DURATION = __ENV.DURATION || '10m';
const MAX_FAIL_RATE = Number(__ENV.MAX_FAIL_RATE || 0.005);
// A chaos drill temporarily makes the fast path slower on purpose (a paused Redis
// becomes a 0.5 s timeout before the database fallback). Keeping the normal SLO here
// would fail the run for the behaviour it is trying to prove.
const READ_P95_MS = Number(__ENV.READ_P95_MS || 200);
const READ_P99_MS = Number(__ENV.READ_P99_MS || 500);
const WRITE_P95_MS = Number(__ENV.WRITE_P95_MS || 300);
const WRITE_P99_MS = Number(__ENV.WRITE_P99_MS || 800);

const tokens = TOKENS_FILE
  ? new SharedArray('tokens', () => JSON.parse(open(TOKENS_FILE)))
  : null;

const ratelimited = new Counter('natiqo_ratelimited_total');
const lessonStarts = new Counter('natiqo_lesson_starts_total');
const lessonsCompleted = new Counter('natiqo_lessons_completed_total');
const sessionStartTrend = new Trend('natiqo_session_start_ms');
const completionTrend = new Trend('natiqo_completion_ms');

export const options = {
  scenarios: {
    browse: {
      executor: 'constant-arrival-rate',
      exec: 'browse',
      rate: Number(__ENV.BROWSE_RPS || 100),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 150,
      maxVUs: 1500,
      tags: { scenario: 'browse' },
    },
    learn: {
      executor: 'constant-arrival-rate',
      exec: 'learn',
      rate: Number(__ENV.LEARN_RPS || 12),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 150,
      maxVUs: 1500,
      tags: { scenario: 'learn' },
    },
    progress: {
      executor: 'constant-arrival-rate',
      exec: 'progress',
      rate: Number(__ENV.PROGRESS_RPS || 18),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 60,
      maxVUs: 600,
      tags: { scenario: 'progress' },
    },
  },
  thresholds: {
    http_req_failed: [`rate<${MAX_FAIL_RATE}`],
    'http_req_duration{kind:read}': [`p(95)<${READ_P95_MS}`, `p(99)<${READ_P99_MS}`],
    'http_req_duration{kind:write}': [`p(95)<${WRITE_P95_MS}`, `p(99)<${WRITE_P99_MS}`],
    checks: ['rate>0.99'],
    natiqo_ratelimited_total: ['count<100'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// ---------------------------------------------------------------- setup

export function setup() {
  const lessonIds = (_parseLessonIds() || collectLessonIds());
  if (!lessonIds.length) {
    throw new Error(
      'No published lesson found. Seed the catalogue first ' +
        '(uv run python -m scripts.seed_content --yes) or pass LESSON_IDS.',
    );
  }

  if (tokens) {
    if (!tokens.length) throw new Error('TOKENS_FILE is empty');
    return { lessonIds, tokenCount: tokens.length };
  }

  if (!USE_DEV_LOGIN) {
    throw new Error('Set TOKENS_FILE or DEV_LOGIN=true');
  }

  const response = http.post(
    `${API}/auth/dev-login`,
    JSON.stringify({
      username: __ENV.DEV_USER || 'admin',
      password: __ENV.DEV_PASSWORD || 'admin',
      device: { platform: 'android', appVersion: 'loadtest' },
    }),
    { headers: { 'Content-Type': 'application/json' }, tags: { kind: 'auth' } },
  );
  if (response.status !== 200) {
    throw new Error(`dev-login failed: ${response.status} ${response.body}`);
  }
  const body = response.json();
  return {
    lessonIds,
    tokenCount: 1,
    devToken: {
      accessToken: body.tokens.accessToken,
      refreshToken: body.tokens.refreshToken,
    },
  };
}

function _parseLessonIds() {
  const raw = (__ENV.LESSON_IDS || '').trim();
  return raw ? raw.split(',').map((value) => value.trim()).filter(Boolean) : null;
}

function collectLessonIds() {
  const response = http.get(`${API}/content/tree`, { tags: { kind: 'read' } });
  if (response.status !== 200) {
    throw new Error(`catalogue unavailable: ${response.status}`);
  }
  const body = response.json();
  const ids = [];
  for (const level of body.items || []) {
    for (const unit of level.units || []) {
      for (const lesson of unit.lessons || []) {
        if (lesson.id) ids.push(lesson.id);
      }
    }
  }
  // The first lesson of the first unit is the only one guaranteed unlocked for a
  // brand-new account. Repeating it is safe: completion is idempotent per sitting.
  return ids.slice(0, 1);
}

// ------------------------------------------------------------- identity

let vuIdentity = null;

function identity(data) {
  if (vuIdentity) return vuIdentity;
  if (data.devToken) {
    vuIdentity = { ...data.devToken };
    return vuIdentity;
  }
  const index = (__VU - 1 + __ITER) % tokens.length;
  const picked = tokens[index];
  vuIdentity = { accessToken: picked.accessToken, refreshToken: picked.refreshToken };
  return vuIdentity;
}

function authHeaders(identity) {
  return { Authorization: `Bearer ${identity.accessToken}` };
}

function refresh(identity) {
  const response = http.post(
    `${API}/auth/refresh`,
    JSON.stringify({ refreshToken: identity.refreshToken }),
    { headers: { 'Content-Type': 'application/json' }, tags: { kind: 'auth' } },
  );
  if (response.status !== 200) {
    // A rotated-from-another-VU token, or an expired family. Drop back to a fresh
    // token from the pool instead of failing the whole iteration.
    vuIdentity = null;
    return false;
  }
  const body = response.json();
  identity.accessToken = body.tokens.accessToken;
  identity.refreshToken = body.tokens.refreshToken;
  return true;
}

function request(method, path, body, headers, kind) {
  const response = http.request(method, `${API}${path}`, body ? JSON.stringify(body) : null, {
    headers,
    tags: { kind },
  });
  if (response.status === 429) {
    ratelimited.add(1);
    const retryAfter = Number(response.headers['Retry-After'] || 1);
    sleep(Math.min(retryAfter, 5));
  }
  return response;
}

function authorized(method, path, body, data, kind) {
  let identity = identity(data);
  let response = request(method, path, body, authHeaders(identity), kind);
  if (response.status === 401) {
    if (refresh(identity)) {
      response = request(method, path, body, authHeaders(identity), kind);
    }
  }
  return response;
}

// ------------------------------------------------------------ scenarios

export function browse(data) {
  const tree = http.get(`${API}/content/tree`, { tags: { kind: 'read' } });
  check(tree, {
    'tree 200': (r) => r.status === 200,
    'tree has items': (r) => {
      try {
        return Array.isArray(r.json('items'));
      } catch (_error) {
        return false;
      }
    },
  });

  const lessonId = data.lessonIds[Math.floor(Math.random() * data.lessonIds.length)];
  const lesson = http.get(`${API}/content/lessons/${lessonId}`, { tags: { kind: 'read' } });
  check(lesson, { 'lesson 200': (r) => r.status === 200 });

  // Revalidate the way a warm client does: one conditional request per iteration.
  const etag = lesson.headers['ETag'];
  if (etag) {
    const conditional = http.get(`${API}/content/lessons/${lessonId}`, {
      headers: { 'If-None-Match': etag },
      tags: { kind: 'read' },
    });
    check(conditional, {
      'etag revalidates': (r) => r.status === 304 || r.status === 200,
    });
  }
  sleep(Math.random() * 0.5);
}

export function learn(data) {
  const lessonId = data.lessonIds[Math.floor(Math.random() * data.lessonIds.length)];
  const started = Date.now();
  const start = authorized(
    'POST',
    '/quiz/sessions',
    { mode: 'lesson', lessonId },
    data,
    'write',
  );
  sessionStartTrend.add(Date.now() - started);
  const startedOk = check(start, {
    'session started': (r) => r.status === 201 || r.status === 200 || r.status === 429,
  });
  if (!startedOk || start.status === 429) return;

  let session;
  try {
    session = start.json();
  } catch (_error) {
    return;
  }
  if (!session || !session.session || !Array.isArray(session.questions)) return;
  lessonStarts.add(1);

  const questions = ANSWERS_PER_SESSION > 0
    ? session.questions.slice(0, ANSWERS_PER_SESSION)
    : session.questions;
  if (!questions.length) return;

  let allAnswered = true;
  for (const question of questions) {
    const answer = answerFor(question);
    if (!answer) {
      allAnswered = false;
      continue;
    }
    const graded = authorized(
      'POST',
      `/quiz/sessions/${session.session.id}`,
      { op: 'answer', questionId: question.id, answer },
      data,
      'write',
    );
    const accepted = check(graded, {
      'answer accepted': (r) => r.status === 200,
    });
    if (!accepted) allAnswered = false;
    if (THINK_TIME_MS > 0) sleep(THINK_TIME_MS / 1000);
  }

  // A partially answered sitting cannot be completed: the server scores the whole
  // sitting. Completing it anyway would turn a rate-limited answer into a 4xx that
  // looks like a server fault, so the run is abandoned here instead.
  if (!allAnswered) {
    authorized(
      'POST',
      `/quiz/sessions/${session.session.id}`,
      { op: 'abandon' },
      data,
      'write',
    );
    return;
  }

  const completionStarted = Date.now();
  const completed = authorized(
    'POST',
    `/quiz/sessions/${session.session.id}`,
    { op: 'complete' },
    data,
    'write',
  );
  completionTrend.add(Date.now() - completionStarted);
  const completedOk = check(completed, {
    'session completed': (r) => r.status === 200 || r.status === 429,
  });
  if (completedOk && completed.status === 200) lessonsCompleted.add(1);
  sleep(Math.random() * 0.3);
}

export function progress(data) {
  const summary = authorized('GET', '/progress', null, data, 'read');
  check(summary, { 'progress 200': (r) => r.status === 200 || r.status === 429 });

  const unlocks = authorized('GET', '/progress/unlocks', null, data, 'read');
  check(unlocks, { 'unlocks 200': (r) => r.status === 200 || r.status === 429 });

  const due = authorized('GET', '/review/due', null, data, 'read');
  check(due, { 'review due 200': (r) => r.status === 200 || r.status === 429 });

  sleep(Math.random() * 1.0);
}

// ---------------------------------------------------------------- helpers

function answerFor(question) {
  const choices = question.choices || [];
  switch (question.kind) {
    case 'multiple_choice':
    case 'true_false':
      return choices.length ? { choice: choices[0] } : null;
    case 'multi_select':
      return choices.length ? { choices: [choices[0]] } : null;
    case 'fill_blank':
    case 'word_bank':
      return { text: choices.length ? choices[0] : 'answer' };
    case 'ordering':
      return choices.length ? { order: choices } : null;
    case 'matching': {
      const prompts = question.prompts || [];
      if (!prompts.length || !choices.length) return null;
      return {
        matching: prompts.map(
          (prompt, index) => `${prompt} => ${choices[index % choices.length]}`,
        ),
      };
    }
    default:
      return choices.length ? { choice: choices[0] } : null;
  }
}

export function handleSummary(data) {
  const result = {
    generatedAt: new Date().toISOString(),
    baseUrl: BASE_URL,
    scenarios: Object.fromEntries(
      Object.entries(data.metrics)
        .filter(([name]) => name.startsWith('http_req_duration') || name === 'http_req_failed')
        .map(([name, metric]) => [name, metric.values]),
    ),
  };
  return {
    stdout: textSummary(data),
    'loadtest-summary.json': JSON.stringify(result, null, 2),
  };
}

function textSummary(data) {
  const lines = ['', 'Natiqo load test', '================'];
  for (const [name, metric] of Object.entries(data.metrics)) {
    if (!name.startsWith('http_req') && !name.startsWith('natiqo_') && name !== 'checks') {
      continue;
    }
    const values = metric.values || {};
    const parts = Object.keys(values)
      .filter((key) => typeof values[key] === 'number')
      .map((key) => `${key}=${Number(values[key]).toFixed(2)}`);
    lines.push(`${name}: ${parts.join(' ')}`);
  }
  return `${lines.join('\n')}\n`;
}
