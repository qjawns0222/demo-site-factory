import os
import json
import logging
import asyncio
import re
import zipfile
import io
import uuid
from typing import Dict, Any, AsyncGenerator

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware

import google.generativeai as genai
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import redis.asyncio as redis
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from data_init import init_db, DB_PATH
import aiosqlite

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIG & SECRETS ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DUMMY_MODE = not bool(GEMINI_API_KEY)
if not DUMMY_MODE:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info(f"Gemini API key configured. Model: {GEMINI_MODEL}")
else:
    logger.warning("GEMINI_API_KEY is not set. Running in DUMMY MODE.")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

# Prompt injection: match multi-line jailbreak patterns, not individual words
INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(previous|all|above)\s+instructions?|"
    r"you\s+are\s+now|"
    r"new\s+instructions?:|"
    r"system\s*:\s*you|"
    r"<\s*system\s*>|"
    r"disregard\s+(all|previous))",
    re.IGNORECASE,
)

# Rate Limiter setup
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Demo Site Generator API (V8)")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client = None

@app.on_event("startup")
async def startup_event():
    global redis_client
    logger.info("Connecting to Redis...")
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("Connected to Redis successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise RuntimeError("CRITICAL SPOF: Redis must be available")

    logger.info("Initializing SQLite database for dynamic prompts...")
    try:
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
    except Exception as e:
        logger.error(f"Failed to initialize SQLite: {e}")
        raise RuntimeError("CRITICAL SPOF: SQLite DB initialization failed")

@app.on_event("shutdown")
async def shutdown_event():
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed.")

def validate_domain(domain: str) -> bool:
    if not domain or len(domain) > 255:
        return False
    return bool(domain.strip())

def verify_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="관리자 기능이 비활성화되어 있습니다. ADMIN_TOKEN 환경변수를 설정하세요.")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="유효하지 않은 관리자 토큰입니다.")

async def redis_scan_keys(pattern: str) -> list[str]:
    """Non-blocking SCAN replacement for KEYS. Safe for production Redis."""
    keys = []
    cursor = 0
    while True:
        cursor, batch = await redis_client.scan(cursor=cursor, match=pattern, count=100)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys

class GenerateRequest(BaseModel):
    domain: str = Field(..., max_length=255)

@app.post("/api/generate")
@limiter.limit("10/minute")
async def start_generation(request: Request, payload: GenerateRequest):
    domain = payload.domain
    if not validate_domain(domain):
        raise HTTPException(status_code=400, detail="유효하지 않은 도메인 형식입니다. 영문/숫자/하이픈만 허용합니다.")

    session_id = str(uuid.uuid4())
    logger.info(f"Starting session {session_id} for domain {domain}")

    await redis_client.setex(f"session_meta:{session_id}:domain", 86400, domain)

    return {"status": "started", "session_id": session_id, "domain": domain}

@app.delete("/api/session/{session_id}")
async def reset_session(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    keys = await redis_scan_keys(f"session:{session_id}:*")
    meta_key = f"session_meta:{session_id}:domain"
    if meta_key not in keys:
        keys.append(meta_key)
    if keys:
        await redis_client.delete(*keys)
    logger.info(f"Reset session {session_id}")
    return {"status": "reset_complete"}

@app.get("/api/sessions")
async def list_sessions():
    """최근 24시간 내 세션 목록 반환"""
    meta_keys = await redis_scan_keys("session_meta:*:domain")
    if not meta_keys:
        return {"sessions": []}

    # Redis pipeline으로 N+1 방지
    pipe = redis_client.pipeline()
    for key in meta_keys:
        pipe.get(key)
    domains = await pipe.execute()

    sessions = []
    for key, domain in zip(meta_keys, domains):
        if domain:
            # key format: session_meta:{session_id}:domain
            parts = key.split(":")
            if len(parts) >= 3:
                session_id = parts[1]
                sessions.append({"session_id": session_id, "domain": domain})

    return {"sessions": sessions}

STEP_NAMES = {
    1: "기능 분석 및 정보 아키텍처",
    2: "UI/UX 비주얼 설계 및 레이아웃 명세",
    3: "에셋 목록 및 배치 전략",
    4: "URL 구조 및 라우팅 설계",
    5: "데이터 모델 및 API 설계",
    6: "컴포넌트 및 서비스 레이어 구현 명세",
    7: "데모용 시드 데이터 설계",
    8: "구현 리스크 및 의존성 지도",
    10: "인터랙티브 데모 생성",
}

@app.get("/api/workflow")
async def get_workflow():
    steps = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # step_id < 100 만 가져옴 (code 모드용 100번대 제외)
            async with db.execute("SELECT step_id FROM prompts WHERE step_id < 100 ORDER BY step_id ASC") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    sid = row[0]
                    steps.append({"id": sid, "name": STEP_NAMES.get(sid, f"단계 {sid}")})
    except Exception as e:
        logger.error(f"Failed to fetch workflow from DB: {e}")

    if not steps:
        steps = [
            {"id": 1, "name": "시스템 아키텍처 및 데이터 흐름 설계"},
            {"id": 2, "name": "프로젝트 폴더 구조 설계"},
            {"id": 3, "name": "명세서 및 인터페이스 설계"},
            {"id": 4, "name": "프론트엔드 구현"},
            {"id": 5, "name": "백엔드 API 구현"},
            {"id": 6, "name": "오류 복원 자동 QA"},
            {"id": 7, "name": "데이터베이스 스키마 설계"},
            {"id": 8, "name": "배포 및 인프라 설계"},
            {"id": 9, "name": "종합 검토 및 최적화"},
        ]

    # 10단계는 DB에 프롬프트 없이 가상으로 추가 (preview 자동생성 단계)
    if steps and not any(s["id"] == 10 for s in steps):
        steps.append({"id": 10, "name": STEP_NAMES[10]})

    return {"steps": steps}

@app.get("/api/session/{session_id}/steps")
async def get_session_steps(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    # 모든 step content를 Redis에서 한 번에 가져오기 (step 10 preview 캐시 포함)
    pipe = redis_client.pipeline()
    for step_id in range(1, 10):
        pipe.get(f"session:{session_id}:step:{step_id}")
    pipe.get(f"session:{session_id}:preview")
    results = await pipe.execute()

    steps = {}
    for i, content in enumerate(results[:9], start=1):
        if content:
            steps[i] = content

    # step 10: preview 캐시가 존재하면 DONE
    if results[9]:
        steps[10] = "인터랙티브 데모가 준비되었습니다."

    return {"session_id": session_id, "domain": domain, "steps": steps}

class SynthesizeRequest(BaseModel):
    content: str = Field(...)

class JSONParsingError(Exception):
    pass

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((JSONParsingError, Exception))
)
async def safe_generate_json(prompt: str) -> dict:
    if DUMMY_MODE:
        await asyncio.sleep(1)
        return {"summary": "Dummy summary - API KEY IS MISSING", "context": "Dummy context"}

    model = genai.GenerativeModel(GEMINI_MODEL)

    generation_config = genai.types.GenerationConfig(
        temperature=0.7,
        response_mime_type="application/json"
    )
    text = ""
    try:
        response = await model.generate_content_async(
            contents=prompt,
            generation_config=generation_config
        )
        text = response.text.strip() if response.text else ""
        if not text:
            raise JSONParsingError("LLM returned an empty response")
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON Parsing failed: {e}. Raw Text: {text!r}")
        raise JSONParsingError(f"Failed to parse LLM response into JSON: {e}")
    except Exception as e:
        logger.error(f"Gemini API Error (Backoff Triggered): {e}")
        raise

@app.post("/api/step/{session_id}/{step_id}/synthesize")
async def synthesize_step(session_id: str, step_id: int, payload: SynthesizeRequest):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="유효하지 않은 세션입니다. (UUID Mismatch)")

    sanitized_content = re.sub(r'[\r\n]+', ' ', payload.content)
    if INJECTION_PATTERNS.search(sanitized_content):
        logger.warning(f"Prompt injection attempt detected on session {session_id}, step {step_id}")
        sanitized_content = "[보안 정책: 프롬프트 조작 패턴이 감지되어 원본 내용을 제거했습니다. 기존 작업만 완수하세요.]"

    await redis_client.set(f"session:{session_id}:step:{step_id}", payload.content, ex=86400)

    # 입력 토큰 절감: 3000자 초과 시 앞부분만 사용
    truncated_content = sanitized_content[:3000] if len(sanitized_content) > 3000 else sanitized_content
    summary_prompt = f"""
    {step_id}단계 승인 내용을 다음 단계를 위해 핵심만 요약하라.
    불필요한 설명 없이 반드시 JSON만 반환: {{"summary": "3문장 이내 핵심 요약", "context": "다음 단계 AI가 알아야 할 핵심 정보 5줄 이내"}}
    내용: {truncated_content}
    """

    try:
        summary_result = await safe_generate_json(summary_prompt)

        context_history_str = await redis_client.get(f"session:{session_id}:context_history")
        context_history = json.loads(context_history_str) if context_history_str else []
        context_history.append({"step_id": step_id, **summary_result})
        await redis_client.set(
            f"session:{session_id}:context_history",
            json.dumps(context_history),
            ex=86400
        )
        await redis_client.set(f"session:{session_id}:context", json.dumps(summary_result), ex=86400)
    except Exception as e:
        logger.error(f"Failed to synthesize for {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Gemini AI 합성 중 지속적 오류 발생")

    # 9단계 완료 시 10단계(인터랙티브 데모) 백그라운드 자동 생성
    if step_id == 9:
        asyncio.create_task(_generate_and_cache_preview(session_id))

    return {"status": "loop_closed", "summary": summary_result}

@app.get("/api/stream_step/{session_id}/{step_id}")
async def stream_step(request: Request, session_id: str, step_id: int):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="유효하지 않은 세션입니다. 해킹된 접근일 수 있습니다.")

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        try:
            db_prompt = ""
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT content FROM prompts WHERE step_id = ?", (step_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        db_prompt = row[0]

            if not db_prompt:
                db_prompt = f"이것은 {step_id}단계의 동적 기본 프롬프트입니다. 도메인 {domain}에 맞게 작업하세요."

            context_history_str = await redis_client.get(f"session:{session_id}:context_history")
            context_str = ""
            if context_history_str:
                history = json.loads(context_history_str)
                recent = history[-2:]  # 최근 2단계만 사용해 토큰 절약
                summaries = [f"[{h['step_id']}단계] {h.get('summary', '')}" for h in recent]
                context_str = "이전 단계 컨텍스트:\n" + "\n".join(summaries)

            full_prompt = f"""
            [{domain}]에 대한 문서 생성 작업
            {context_str}

            {db_prompt}
            """

            generated_content = ""

            if DUMMY_MODE:
                dummy_tokens = f"DUMMY DATA STREAM: API 코드가 없습니다. 도메인 {domain}용 가상응답 {step_id}".split(" ")
                for token in dummy_tokens:
                    if await request.is_disconnected():
                        logger.info("Client disconnected.")
                        break
                    yield {"event": "chunk", "data": json.dumps({"text": token + " "})}
                    generated_content += token + " "
                    await asyncio.sleep(0.3)
            else:
                model = genai.GenerativeModel(GEMINI_MODEL)
                response = await model.generate_content_async(full_prompt, stream=True)
                async for chunk in response:
                    if await request.is_disconnected():
                        logger.info("Client stream disconnected early.")
                        break
                    if chunk.text:
                        yield {"event": "chunk", "data": json.dumps({"text": chunk.text})}
                        generated_content += chunk.text

            if not await request.is_disconnected():
                await redis_client.set(f"session:{session_id}:step:{step_id}", generated_content, ex=86400)
                yield {"event": "completed", "data": json.dumps({"content": generated_content})}
                yield {"event": "finished", "data": json.dumps({"status": "done"})}

        except Exception as e:
            err_str = str(e)
            logger.error(f"Streaming error in step {step_id}: {err_str}", exc_info=True)
            # 에러 원인별 사용자 메시지 분리
            if "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower():
                user_msg = "API 사용량 한도 초과 (429) — 잠시 후 다시 시도하세요."
            elif "timeout" in err_str.lower() or "deadline" in err_str.lower():
                user_msg = "연결 시간 초과 — 네트워크 상태를 확인하고 다시 시도하세요."
            elif "blocked" in err_str.lower() or "safety" in err_str.lower():
                user_msg = "콘텐츠 안전 정책으로 차단됨 — 도메인 주제를 변경해 보세요."
            else:
                user_msg = f"서버 통신 실패 — 다시 시도하세요. ({err_str[:80]})"
            yield {"event": "error", "data": json.dumps({"error": user_msg})}

    return EventSourceResponse(event_generator())

@app.get("/api/export/{session_id}")
async def export_zip(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션 만료이거나 유효하지 않은 UUID 접근입니다.")

    keys = await redis_scan_keys(f"session:{session_id}:step:*")
    if not keys:
        raise HTTPException(status_code=404, detail="추출할 단계별 데이터가 없습니다. 먼저 Workflow를 시작하십시오.")

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{domain}_report.md", f"# AI Demo Site Report\n\nDomain: {domain}\nSession ID: {session_id}\n")

        step_summaries = []
        for key in sorted(keys, key=lambda k: int(k.split(":")[-1])):
            content = await redis_client.get(key)
            if not content: continue

            step_id_str = key.split(":")[-1]
            try:
                step_num = int(step_id_str)
                if step_num >= 100:
                    continue  # code 모드 프롬프트는 ZIP에서 제외
                step_name = STEP_NAMES.get(step_num, f"단계 {step_num}")
            except ValueError:
                step_name = f"단계 {step_id_str}"
            zf.writestr(f"step_{step_id_str}.md", content)
            step_summaries.append((step_id_str, step_name))

            pattern = re.compile(r"```([a-zA-Z0-9_+\-]+)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
            blocks = pattern.findall(content)

            for index, (lang, code_content) in enumerate(blocks):
                ext_map = {"python": "py", "html": "html", "javascript": "js", "typescript": "ts", "typescriptreact": "tsx"}
                lang_clean = lang.strip().lower() if lang else "txt"
                ext = ext_map.get(lang_clean, lang_clean)

                safe_name = f"codeblock_{step_id_str}_{index}.{ext}"
                try:
                    zf.writestr(f"src/{safe_name}", code_content)
                except Exception as e:
                    logger.error(f"Failed to extract safe file {safe_name}: {e}")

        # PROMPT.md 생성 — AI 코딩 도구용 구현 지시 파일
        file_list = "\n".join([f"- step_{sid}.md : {name}" for sid, name in step_summaries])
        prompt_md = f"""# {domain} — AI 구현 프롬프트

이 ZIP 파일에는 "{domain}" 서비스의 설계 문서가 포함되어 있습니다.
아래 파일들을 컨텍스트로 읽고, 완전히 실행 가능한 프로젝트를 구현해주세요.

## 포함된 설계 문서

{file_list}

## 구현 지시사항

1. 위 문서들을 순서대로 모두 읽어 전체 설계를 파악하세요.
2. step_5.md (데이터 모델/API)와 step_6.md (구현 명세)를 핵심 기준으로 삼으세요.
3. step_8.md (리스크/의존성)를 참고해 구현 순서와 주의사항을 확인하세요.
4. 프로젝트 구조를 먼저 생성한 뒤, 파일별로 순차 구현하세요.
5. 각 파일 구현 후 다른 파일과의 인터페이스가 일치하는지 확인하세요.

## 권장 기술 스택

- Frontend: Next.js (TypeScript), Tailwind CSS
- Backend: FastAPI (Python) 또는 Next.js API Routes
- Database: PostgreSQL 또는 SQLite (개발용)
- 배포: Docker Compose

## 시작점

`step_8.md`의 "구현 시작점 권장" 섹션을 먼저 확인하고 그 순서에 따라 구현을 시작하세요.
"""
        zf.writestr("PROMPT.md", prompt_md)

    memory_file.seek(0)
    safe_domain_filename = re.sub(r'[^a-zA-Z0-9_\-]', '_', domain)
    return StreamingResponse(
        memory_file,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={safe_domain_filename}_project.zip"}
    )

async def _build_preview_html(session_id: str, domain: str, user_requirements: str = "") -> str:
    """spec 수집 → Gemini 호출 → HTML 반환 (캐싱 없음, 순수 생성)
    비용 최적화: 전체 step 텍스트 대신 synthesize된 context_history 요약만 사용
    """
    # context_history: 각 단계 synthesize 결과 요약 (전체 텍스트보다 ~70% 적은 토큰)
    context_history_str = await redis_client.get(f"session:{session_id}:context_history")
    if context_history_str:
        context_history = json.loads(context_history_str)
        spec_parts = []
        for entry in context_history:
            step_id = entry.get("step_id", "?")
            step_name = STEP_NAMES.get(int(step_id), f"단계 {step_id}")
            summary = entry.get("summary", "")
            context = entry.get("context", "")
            spec_parts.append(f"=== {step_name} ===\n{summary}\n{context}".strip())
        spec_text = "\n\n".join(spec_parts)
    else:
        # fallback: context_history 없으면 step 원문 사용
        keys = await redis_scan_keys(f"session:{session_id}:step:*")
        keys_sorted = sorted(
            [k for k in keys if int(k.split(":")[-1]) < 100],
            key=lambda k: int(k.split(":")[-1])
        )
        spec_parts = []
        for key in keys_sorted:
            content = await redis_client.get(key)
            if not content:
                continue
            step_num = key.split(":")[-1]
            step_name = STEP_NAMES.get(int(step_num), f"단계 {step_num}")
            spec_parts.append(f"=== {step_name} ===\n{content}")
        spec_text = "\n\n".join(spec_parts)

    if DUMMY_MODE:
        return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><title>{domain} — Demo</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px;}}h1{{color:#333;}}p{{color:#666;}}</style></head>
<body><h1>{domain}</h1><p>더미 모드 — GEMINI_API_KEY를 설정하면 실제 데모가 생성됩니다.</p></body></html>"""

    extra_req_block = ""
    if user_requirements and user_requirements.strip():
        extra_req_block = f"""
## 사용자 추가 요구사항 (최우선 반영)

아래 요구사항은 일반 가이드라인보다 우선합니다. 반드시 반영하세요:

{user_requirements.strip()}

---
"""

    prompt = f"""You are a senior frontend engineer at a top-tier SaaS company (think Vercel, Linear, Notion). Your specialty is building beautiful, fully-interactive single-file HTML demos that look indistinguishable from real products. Generate a single-file interactive HTML demo for "{domain}" service.
{extra_req_block}
## Service Specification

{spec_text}

---

## LAYOUT SELECTION — Choose the best fit for "{domain}"

First, analyze the domain and pick ONE layout archetype that makes the most sense for real users of this service. Do NOT default to a generic dashboard.

**Available layout archetypes:**

| Archetype | Best for | Key UI pattern |
|-----------|----------|----------------|
| **DASHBOARD** | Analytics, finance, operations | KPI cards + charts + data table |
| **KANBAN** | Project management, tasks, CRM pipeline | Drag-able columns (Todo/In Progress/Done) |
| **FEED** | Social, news, content, community | Infinite scroll cards + compose box |
| **CALENDAR** | Scheduling, booking, events, HR leave | Month/week grid + event slots |
| **GALLERY** | Portfolio, marketplace, e-commerce, media | Masonry/grid cards + filters sidebar |
| **CHAT** | Messaging, support, collaboration | Left contact list + right message thread |
| **TIMELINE** | Logistics, delivery, history, audit log | Vertical timeline + status steps |
| **FORM-WIZARD** | Onboarding, survey, application, checkout | Multi-step form + progress bar |

Pick the archetype that a real product designer would choose for "{domain}". State your choice as an HTML comment on line 2: `<!-- LAYOUT: KANBAN -->`.

## REQUIRED CODE STRUCTURE

Regardless of layout, your JavaScript MUST include these sections. ALL sections are MANDATORY — do not omit or leave as stubs.

```
// =============================================
// 1. DUMMY DATA — MANDATORY: minimum 15 items, ALL fields populated
// =============================================
// RULES:
// - Design the object shape to fit "{domain}" exactly — don't use generic fields
// - Every object MUST have at least 6 domain-appropriate fields
// - Field names must reflect the real domain (e.g. HR→ 직급/부서/입사일, logistics→ 운송번호/출발지/도착지/무게)
// - Use real Korean names, real-looking dates (2024-2025), realistic amounts/numbers
// - Include varied status values that match the domain workflow
// - NO placeholder names like "홍길동1", "항목A", "사용자1", "테스트"
// - All 15 items must be unique — no copy-paste with only one field changed
const DUMMY_DATA = [
  // 15 items with domain-specific fields for "{domain}"
];

// =============================================
// 2. APP STATE
// =============================================
const state = {{
  currentView: 'main',
  currentItem: null,
  filteredData: [...DUMMY_DATA],
  searchQuery: '',
}};

// =============================================
// 3. VIEW SWITCHER
// =============================================
function showView(viewName) {{
  document.querySelectorAll('.view-section').forEach(el => el.style.display = 'none');
  const target = document.getElementById('view-' + viewName);
  if (target) target.style.display = 'block';
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const navItem = document.querySelector('[data-view="' + viewName + '"]');
  if (navItem) navItem.classList.add('active');
  state.currentView = viewName;
}}

// =============================================
// 4. MODAL
// =============================================
function openModal(item) {{
  state.currentItem = item;
  // MANDATORY: populate modal with item data before showing
  // Example: document.getElementById('modal-title').textContent = item.name;
  const modal = document.getElementById('detail-modal');
  if (modal) modal.style.display = 'flex';
}}
function closeModal() {{
  const modal = document.getElementById('detail-modal');
  if (modal) modal.style.display = 'none';
  state.currentItem = null;
}}

// =============================================
// 5. RENDER — MANDATORY: implement fully for chosen layout
// =============================================
// This function MUST produce visible DOM content immediately.
// NEVER leave this as a stub or empty function.
//
// For DASHBOARD: render KPI cards + table rows with DUMMY_DATA
// For KANBAN:    distribute items into columns by status field
// For FEED:      render post/card for each item with author, content, timestamp
// For CALENDAR:  render event dots/blocks on date cells
// For GALLERY:   render image cards in grid with title, price, tags
// For CHAT:      render contact list entries; clicking one loads message thread
// For TIMELINE:  render vertical steps with status icons and timestamps
// For FORM-WIZARD: render current step fields, update progress bar
//
// EVERY card/row MUST have an onclick that calls openModal(item) with the item object.
// Use JSON.stringify carefully: openModal(${{JSON.stringify(item).replace(/'/g, "&#39;").replace(/"/g, '&quot;')}})
function renderContent(data) {{
  // IMPLEMENT THIS FULLY — no stubs
}}

// =============================================
// 6. SEARCH / FILTER
// =============================================
function handleSearch(query) {{
  state.searchQuery = query;
  state.filteredData = DUMMY_DATA.filter(item =>
    Object.values(item).some(v => String(v).toLowerCase().includes(query.toLowerCase()))
  );
  renderContent(state.filteredData);
}}

// =============================================
// 7. FORM SUBMIT — wire to id="add-form"
// =============================================
function handleFormSubmit(e) {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const newItem = {{ id: Date.now() }};
  fd.forEach((v, k) => {{ newItem[k] = v; }});
  DUMMY_DATA.unshift(newItem);
  state.filteredData = [...DUMMY_DATA];
  renderContent(state.filteredData);
  e.target.reset();
  showToast('성공적으로 등록되었습니다.');
  closeModal();
}}

// =============================================
// 8. TOAST
// =============================================
function showToast(message) {{
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.style.display = 'block';
  setTimeout(() => {{ toast.style.display = 'none'; }}, 2500);
}}

// =============================================
// 9. INIT — runs on DOMContentLoaded
// =============================================
document.addEventListener('DOMContentLoaded', () => {{
  renderContent(DUMMY_DATA);  // MUST call with data — never empty

  const searchEl = document.getElementById('search-input');
  if (searchEl) searchEl.addEventListener('input', e => handleSearch(e.target.value));

  const form = document.getElementById('add-form');
  if (form) form.addEventListener('submit', handleFormSubmit);

  const modal = document.getElementById('detail-modal');
  if (modal) modal.addEventListener('click', e => {{ if (e.target === e.currentTarget) closeModal(); }});

  // Wire ALL buttons with data-action attributes
  document.querySelectorAll('[data-action]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const action = btn.dataset.action;
      if (action === 'open-add-modal') openModal(null);
      if (action === 'close-modal') closeModal();
    }});
  }});
}});
```

## REQUIRED HTML ELEMENTS

Your HTML MUST always include:
- `id="detail-modal"` — modal/panel (initial `style="display:none"`)
- `id="toast"` — toast notification (initial `style="display:none"`)
- `id="search-input"` — search input (can be in navbar or sidebar)
- `id="add-form"` — form for new item creation
- `.view-section` + `.nav-item[data-view]` — if using multi-view navigation
- Modal close: `<button onclick="closeModal()">×</button>`

Layout-specific required elements:
- KANBAN: `id="col-todo"`, `id="col-inprogress"`, `id="col-done"`
- FEED: `id="feed-container"`
- CALENDAR: `id="calendar-grid"`
- GALLERY: `id="gallery-container"`
- CHAT: `id="message-thread"`, `id="contact-list"`
- TIMELINE: `id="timeline-container"`
- DASHBOARD: `id="list-container"`, KPI cards with real numbers
- FORM-WIZARD: `id="wizard-steps"`, progress indicator

## REQUIRED SELECT ELEMENTS

Every `<select>` must have minimum 3 real `<option>` values:
```html
<select name="category">
  <option value="">카테고리 선택</option>
  <option value="type1">실제값1</option>
  <option value="type2">실제값2</option>
  <option value="type3">실제값3</option>
</select>
```

## DESIGN REQUIREMENTS

### Visual Style — Dark SaaS (Vercel/Linear quality)
- Use the CSS variables defined below for ALL colors — no hardcoded hex values in HTML/CSS
- Accent color: pick ONE that fits the domain character (finance→indigo, health→emerald, logistics→amber, social→violet, alerts→rose)
- Use `var(--accent)` ONLY for: active nav indicator, primary button, focus ring, status badges — not as background fills

### Typography
- Import Inter from Google Fonts: `<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">`
- Apply globally: `font-family: 'Inter', -apple-system, sans-serif;`
- Headings: `font-weight: 600`, body: `font-weight: 400`
- Use numeric font-size scale: 12px labels, 14px body, 16px subheadings, 20-24px headings

### Layout — App Shell
- Full-height layout: `height: 100vh; display: flex; flex-direction: column; overflow: hidden;`
- Top navbar: `height: 56px`, logo left, actions right, `border-bottom: 1px solid var(--border)`
- Left sidebar: `width: 220px`, collapsible nav items with icons
- Main content area: `flex: 1; overflow-y: auto; padding: 24px;`
- Content max-width: `1200px; margin: 0 auto;`

### Component Quality
- Cards: `border-radius: 8px; border: 1px solid var(--border); background: var(--surface); padding: 20px;`
- Metric cards (KPI): large number `font-size: 28px; font-weight: 700; color: var(--text)`, label below in `var(--text-muted)`, trend badge (↑ +12%) in green/red
- Tables: zebra rows `var(--surface)` / `var(--bg)`, sticky header, row hover `var(--surface-2)`
- Buttons: primary `background: var(--accent); color: #fff; border-radius: 6px; padding: 8px 16px; font-weight: 500; border: none`
          secondary `background: transparent; border: 1px solid var(--border-hover); color: var(--text-muted)`
- Badges/tags: `border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 500`
- Nav items: icon + label, active state `background: var(--surface-2); border-left: 2px solid var(--accent); color: var(--text)`
- Input fields: `background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; color: var(--text)`
              focus: `border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px var(--accent-subtle)`

### Micro-interactions
- All interactive elements: `transition: all 0.15s ease`
- Button hover: `filter: brightness(1.15); cursor: pointer`
- Row hover: `background: var(--surface-2)`
- Nav hover: `background: var(--surface-2); color: var(--text)`
- Modal backdrop: `background: rgba(0,0,0,0.75)`, card `border-radius: 12px; border: 1px solid var(--border)`
- Toast: fixed bottom-right, `background: var(--surface); border: 1px solid var(--border); color: var(--text)`, slide-up keyframe animation

### Do NOT
- No default browser styles (reset everything)
- No Comic Sans, Arial, or system-ui as primary font
- No flat single-color backgrounds for entire page
- No unstyled `<select>` or `<input>` — style them all
- No missing hover states
- No Lorem ipsum — all text must be domain-specific Korean

### CDN Resources
- **DO NOT use Tailwind CSS** — use plain CSS only (avoids class/inline-style conflicts)
- Font Awesome 6: `<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">`
- Inter font: Google Fonts (above)
- All UI text in Korean

### CSS Variables — define these in `:root` and use them throughout
```css
:root {{
  --bg: #0a0a0a;
  --surface: #111111;
  --surface-2: #1a1a1a;
  --border: #2a2a2a;
  --border-hover: #3a3a3a;
  --text: #ffffff;
  --text-muted: #a1a1aa;
  --text-dim: #71717a;
  --accent: /* pick ONE: #6366f1 or #8b5cf6 or #10b981 or #f59e0b or #f43f5e based on domain */;
  --accent-subtle: /* accent color at 15% opacity */;
}}
```
Use `var(--accent)` everywhere instead of hardcoded accent color.

## OUTPUT FORMAT

Output ONLY the complete HTML file.
Start exactly with `<!DOCTYPE html>` and end with `</html>`.
No explanation, no markdown code fences, no comments outside the HTML."""

    # 비용 최적화: preview 생성은 1.5-flash 사용 (2.5-flash 대비 ~50% 절감)
    preview_model = os.environ.get("GEMINI_PREVIEW_MODEL", "gemini-1.5-flash")
    model = genai.GenerativeModel(preview_model)
    response = await model.generate_content_async(prompt)

    if (not response.candidates or
        not response.candidates[0].content or
        not response.candidates[0].content.parts):
        raise ValueError("Gemini가 빈 응답을 반환했습니다.")

    generated = response.text.strip()
    if generated.startswith("```html"):
        generated = generated[7:]
    if generated.startswith("```"):
        generated = generated[3:]
    if generated.endswith("```"):
        generated = generated[:-3]
    return generated.strip()


async def _generate_and_cache_preview(session_id: str, user_requirements: str = ""):
    """9단계 synthesize 완료 후 백그라운드에서 preview 생성 + Redis 캐싱"""
    status_key = f"session:{session_id}:preview_status"
    try:
        domain = await redis_client.get(f"session_meta:{session_id}:domain")
        if not domain:
            return
        # 이미 캐시 있으면 스킵 (user_requirements가 있으면 강제 재생성이므로 스킵 안 함)
        if not user_requirements:
            existing = await redis_client.get(f"session:{session_id}:preview")
            if existing:
                return
        logger.info(f"[Preview] Background generation started for {session_id}")
        await redis_client.set(status_key, "generating", ex=3600)
        html = await asyncio.wait_for(
            _build_preview_html(session_id, domain, user_requirements=user_requirements),
            timeout=600  # 10분
        )
        await redis_client.set(f"session:{session_id}:preview", html, ex=86400)
        await redis_client.delete(status_key)
        logger.info(f"[Preview] Cached for {session_id} ({len(html)} chars)")
    except asyncio.TimeoutError:
        logger.error(f"[Preview] Generation timed out for {session_id}")
        await redis_client.set(status_key, "error", ex=300)
    except Exception as e:
        import traceback
        logger.error(f"[Preview] Background generation failed for {session_id}: {e}\n{traceback.format_exc()}")
        await redis_client.set(status_key, "error", ex=300)


@app.post("/api/preview/{session_id}/generate")
async def preview_generate(session_id: str):
    """캐시가 없을 때 preview 생성을 명시적으로 시작 (10단계 수동 재시도용)"""
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    existing = await redis_client.get(f"session:{session_id}:preview")
    if existing:
        return {"status": "already_ready"}
    status_key = f"session:{session_id}:preview_status"
    current_status = await redis_client.get(status_key)
    if current_status == "generating":
        return {"status": "already_generating"}
    asyncio.create_task(_generate_and_cache_preview(session_id))
    return {"status": "started"}


class RegenerateRequest(BaseModel):
    user_requirements: str = ""


@app.post("/api/preview/{session_id}/regenerate")
async def preview_regenerate(session_id: str, body: RegenerateRequest = RegenerateRequest()):
    """캐시를 삭제하고 preview를 강제 재생성"""
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    await redis_client.delete(f"session:{session_id}:preview")
    asyncio.create_task(_generate_and_cache_preview(session_id, user_requirements=body.user_requirements))
    return {"status": "regenerating"}


@app.get("/api/preview/{session_id}/status")
async def preview_status(session_id: str):
    """10단계 상태 확인: ready / generating / error"""
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    cached = await redis_client.get(f"session:{session_id}:preview")
    if cached:
        return {"status": "ready"}
    status = await redis_client.get(f"session:{session_id}:preview_status")
    if status == "error":
        return {"status": "error"}
    return {"status": "generating"}


@app.get("/api/preview/{session_id}/source")
async def preview_source(session_id: str):
    """HTML 소스코드 텍스트 반환"""
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    cached = await redis_client.get(f"session:{session_id}:preview")
    if cached:
        return {"html": cached}
    raise HTTPException(status_code=404, detail="아직 생성 중입니다. 잠시 후 다시 시도하세요.")


@app.get("/api/preview/{session_id}", response_class=HTMLResponse)
async def preview_html(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션 만료이거나 유효하지 않은 UUID 접근입니다.")

    # 캐시 우선
    cached = await redis_client.get(f"session:{session_id}:preview")
    if cached:
        return HTMLResponse(content=cached)

    # 캐시 없으면 즉시 생성
    keys = await redis_scan_keys(f"session:{session_id}:step:*")
    if not keys:
        raise HTTPException(status_code=404, detail="프리뷰할 데이터가 없습니다. 먼저 Workflow를 시작하십시오.")

    try:
        html = await _build_preview_html(session_id, domain)
        await redis_client.set(f"session:{session_id}:preview", html, ex=86400)
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Preview generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"데모 생성 실패: {str(e)}")

# --- Admin Endpoints ---

class PromptUpdateRequest(BaseModel):
    content: str = Field(..., min_length=1)

@app.get("/api/admin/prompts")
async def get_prompts(x_admin_token: str = Header(default="")):
    verify_admin_token(x_admin_token)
    prompts = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT step_id, content FROM prompts ORDER BY step_id ASC") as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                prompts.append({"step_id": row[0], "content": row[1]})
    return {"prompts": prompts}

@app.put("/api/admin/prompts/{step_id}")
async def update_prompt(step_id: int, payload: PromptUpdateRequest, x_admin_token: str = Header(default="")):
    verify_admin_token(x_admin_token)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO prompts (step_id, content) VALUES (?, ?) ON CONFLICT(step_id) DO UPDATE SET content = excluded.content",
            (step_id, payload.content)
        )
        await db.commit()
    logger.info(f"Admin updated prompt for step {step_id}")
    return {"status": "updated", "step_id": step_id}


# ─── 기획서 생성 API ───────────────────────────────────────────────────────────

class PlanPagesRequest(BaseModel):
    session_id: str

class PlanPagesReviseRequest(BaseModel):
    session_id: str
    pages: list[dict]
    comment: str

class PlanGenerateRequest(BaseModel):
    session_id: str
    page_name: str
    mode: str = "single"  # "single" | "all"


async def _get_plan_context(session_id: str) -> tuple[str, str]:
    """기획서 생성용 컨텍스트 수집: Step 1, 2, 4, 6 synthesize 요약 반환"""
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    context_history_str = await redis_client.get(f"session:{session_id}:context_history")
    context_parts = []

    if context_history_str:
        history = json.loads(context_history_str)
        target_steps = {1, 2, 4, 6}
        for entry in history:
            step_id = int(entry.get("step_id", 0))
            if step_id in target_steps:
                step_name = STEP_NAMES.get(step_id, f"단계 {step_id}")
                summary = entry.get("summary", "")
                context = entry.get("context", "")
                context_parts.append(f"=== {step_name} ===\n{summary}\n{context}".strip())
    else:
        # fallback: synthesize 없으면 step 원문 사용
        for step_id in [1, 2, 4, 6]:
            content = await redis_client.get(f"session:{session_id}:step:{step_id}")
            if content:
                step_name = STEP_NAMES.get(step_id, f"단계 {step_id}")
                context_parts.append(f"=== {step_name} ===\n{content[:2000]}")

    return domain, "\n\n".join(context_parts)


@app.post("/api/plan/pages")
async def get_plan_pages(payload: PlanPagesRequest):
    """two-pass 방식으로 페이지 목록 생성:
    1차: 사용자 유형 + 핵심 도메인 추출
    2차: 도메인별 전체 화면 목록 생성
    """
    domain, context = await _get_plan_context(payload.session_id)

    if DUMMY_MODE:
        return {"pages": [
            {"name": "로그인", "description": "이메일/비밀번호로 로그인"},
            {"name": "회원가입", "description": "신규 계정 생성"},
            {"name": "비밀번호 찾기", "description": "이메일로 비밀번호 재설정 링크 발송"},
            {"name": "메인 홈", "description": "서비스 진입점, 핵심 기능 소개"},
            {"name": "목록 조회", "description": "전체 항목 조회 및 필터링"},
            {"name": "상세 조회", "description": "개별 항목 상세 정보 확인"},
            {"name": "항목 등록", "description": "새 항목 작성 및 저장"},
            {"name": "항목 수정", "description": "기존 항목 내용 편집"},
            {"name": "항목 삭제 확인", "description": "삭제 전 확인 모달"},
            {"name": "프로필 조회", "description": "내 계정 정보 확인"},
            {"name": "프로필 수정", "description": "닉네임, 사진 등 계정 정보 수정"},
            {"name": "비밀번호 변경", "description": "현재 비밀번호 확인 후 새 비밀번호로 변경"},
            {"name": "회원 탈퇴", "description": "계정 영구 삭제 확인 및 처리"},
            {"name": "관리자 대시보드", "description": "전체 서비스 현황 및 주요 지표 확인"},
            {"name": "관리자 사용자 목록", "description": "전체 회원 목록 조회 및 검색"},
            {"name": "관리자 사용자 상세", "description": "특정 회원 정보 조회 및 제재 처리"},
            {"name": "관리자 콘텐츠 목록", "description": "전체 콘텐츠 조회 및 관리"},
        ]}

    # ── 1차 호출: 사용자 유형 + 핵심 도메인 파악 ──────────────────────────────
    pass1_prompt = f"""당신은 서비스 기획자입니다. 아래 "{domain}" 서비스 설계 컨텍스트를 읽고 두 가지를 JSON으로 반환하세요.

## 서비스 설계 컨텍스트
{context}

## 추출할 내용

1. **user_types**: 이 서비스를 사용하는 모든 사용자 유형
   - 예: ["비로그인 사용자", "일반 회원", "판매자", "관리자(Admin)"]

2. **domains**: 이 서비스에서 데이터를 생성/조회/수정/삭제하는 모든 핵심 도메인(엔티티)
   - 예: ["상품", "주문", "리뷰", "쿠폰", "공지사항"]
   - 인증(로그인/회원가입)과 사용자 계정(프로필)은 항상 포함하세요.
   - 관리자가 있다면 관리자가 관리하는 도메인도 포함하세요.

반드시 JSON만 반환: {{"user_types": ["..."], "domains": ["..."]}}
"""

    pass1 = await safe_generate_json(pass1_prompt)
    user_types = pass1.get("user_types", [])
    domains = pass1.get("domains", [])

    # 1차 실패 시 폴백: 기본값으로 진행
    if not user_types:
        user_types = ["비로그인 사용자", "일반 회원", "관리자"]
    if not domains:
        logger.warning(f"[plan/pages] pass1 domain extraction failed for session {payload.session_id}, falling back")
        raise HTTPException(status_code=500, detail="서비스 도메인 분석 실패. 워크플로우 단계를 더 진행한 후 시도해주세요.")

    user_types_str = "\n".join(f"- {u}" for u in user_types)
    domains_str = "\n".join(f"- {d}" for d in domains)

    # ── 2차 호출: 도메인별 전체 화면 목록 생성 ────────────────────────────────
    pass2_prompt = f"""당신은 서비스 기획자입니다. 아래 분석된 "{domain}" 서비스의 사용자 유형과 도메인을 바탕으로 구현해야 할 모든 화면 목록을 만드세요.

## 서비스 설계 컨텍스트 (참고)
{context[:2000]}

## 사용자 유형
{user_types_str}

## 핵심 도메인
{domains_str}

## 화면 생성 규칙

각 도메인에 대해 다음을 확인하세요:
- 인증 도메인: 로그인 / 회원가입 / 비밀번호 찾기 / 비밀번호 재설정
- 사용자 계정 도메인: 프로필 조회 / 프로필 수정 / 비밀번호 변경 / 회원 탈퇴
- 일반 도메인: 목록 / 상세 / 등록 / 수정 / 삭제 확인 (해당되는 것만)
- 관리자 도메인: 관리자 대시보드 + 각 관리 도메인별 목록/상세/처리 화면

화면 이름 규칙:
- 기능 단위 하나씩 (금지: "마이페이지", "상품관리" / 허용: "상품 목록", "상품 등록")
- description은 이 화면에서 사용자가 할 수 있는 핵심 행동 1줄
- 서비스 컨텍스트에 있는 도메인 특유의 용어와 기능을 반영하세요

반드시 JSON만 반환: {{"pages": [{{"name": "화면명", "description": "설명"}}]}}
"""

    result = await safe_generate_json(pass2_prompt)
    pages = result.get("pages", [])
    if not isinstance(pages, list):
        pages = []
    return {"pages": pages}


@app.post("/api/plan/pages/revise")
async def revise_plan_pages(payload: PlanPagesReviseRequest):
    """코멘트를 반영해 페이지 목록을 재생성"""
    domain, context = await _get_plan_context(payload.session_id)

    if DUMMY_MODE:
        return {"pages": payload.pages + [{"name": "추가 페이지", "description": payload.comment}]}

    current_pages_str = "\n".join([f"- {p['name']}: {p.get('description', '')}" for p in payload.pages])

    prompt = f"""당신은 서비스 기획자입니다. 아래 현재 화면 목록을 사용자 코멘트에 맞게 수정하여 JSON으로 반환하세요.

## 현재 화면 목록
{current_pages_str}

## 사용자 코멘트
{payload.comment}

## 서비스 컨텍스트 (참고용)
{context[:1500]}

## 지시사항
- 코멘트를 최우선으로 반영하세요.
- 화면은 **기능 단위** 하나씩입니다. 메뉴 묶음("마이페이지", "상품관리" 등)은 기능 단위로 분해하세요.
- CRUD 도메인은 목록/상세/등록/수정/삭제확인 화면이 모두 있는지 확인하고 누락된 것은 추가하세요.
- 코멘트로 추가 요청된 기능은 해당 도메인의 전체 CRUD 화면을 함께 추가하세요.
- 반드시 JSON만 반환: {{"pages": [{{"name": "화면명", "description": "설명"}}]}}
"""

    result = await safe_generate_json(prompt)
    pages = result.get("pages", [])
    if not isinstance(pages, list):
        pages = payload.pages
    return {"pages": pages}


@app.post("/api/plan/generate")
async def generate_plan_page(payload: PlanGenerateRequest):
    """특정 페이지의 기획서 섹션을 생성 (ASCII 레이아웃 + 번호별 인터랙션 명세)"""
    domain, context = await _get_plan_context(payload.session_id)
    page_name = payload.page_name

    if DUMMY_MODE:
        return {"content": f"""## {page_name}

### 화면 레이아웃

```
┌─────────────────────────────────────┐
│  ① [검색바                    🔍]   │
├─────────────────────────────────────┤
│  ② [필터A] [필터B] [필터C]          │
├──────────────┬──────────────────────┤
│ ③ 카드       │ ③ 카드               │
│   [④ 버튼]  │   [④ 버튼]           │
└──────────────┴──────────────────────┘
```

### 요소별 인터랙션 명세

① 검색바
   - 입력 시 실시간 자동완성 드롭다운 표시
   - 엔터/검색 버튼 클릭 시 결과 필터링

② 필터 탭
   - 클릭 시 해당 카테고리로 목록 필터링
   - 활성 탭 하이라이트 표시

③ 콘텐츠 카드
   - 클릭 시 상세 페이지로 이동
   - hover 시 그림자 강조 효과

④ 액션 버튼
   - 클릭 시 저장 완료 토스트 팝업 표시
   - 재클릭 시 취소 확인 모달 표시
"""}

    prompt = f"""당신은 시니어 프로덕트 매니저입니다. "{domain}" 서비스의 "{page_name}" 페이지 기획서를 아래 형식에 맞게 Markdown으로 작성하세요.

## 서비스 설계 컨텍스트
{context}

---

## 작성 규칙

- 모든 섹션을 빠짐없이 작성하세요. 섹션 생략 또는 "추후 결정" 표현 금지.
- 추상적·일반적 설명 금지. 도메인 특화 용어와 구체적 수치/조건을 사용하세요.
- 표는 모든 셀을 채우세요.
- 섹션 0(서비스 개요)은 모든 페이지 기획서에 반드시 포함하세요. 페이지별로 내용이 동일해도 됩니다.

---

## 출력 형식 (반드시 아래 구조 그대로 출력)

## {page_name}

### 0. 서비스 개요

이 섹션은 페이지 기획서를 읽는 사람이 서비스의 맥락을 즉시 이해할 수 있도록 반드시 작성합니다.

- **서비스 배경 및 문제 정의**: 이 서비스가 해결하려는 문제와 존재 이유를 2~3문장으로 서술 (예: "기존 뉴스 매체는 속보성 커뮤니티 이슈를 다루지 못한다. {domain}은 유튜브/레딧/인스타그램 등 소셜 채널의 실시간 이슈를 큐레이션해 빠르게 전달한다.")
- **핵심 타겟 사용자**: 주요 사용자 유형 2~3개를 구체적으로 서술 (예: "① 20~30대 직장인 — 출퇴근 중 트렌드 빠르게 파악, ② 콘텐츠 크리에이터 — 화제 이슈 소재 탐색")
- **차별점 및 핵심 가치**: 경쟁 서비스 대비 이 서비스만의 차별점 2~3가지 (예: "① 커뮤니티 원문 링크 제공으로 1차 출처 접근 가능, ② AI 요약으로 맥락 파악 시간 단축")
- **비즈니스 목표**: 이 서비스가 달성하려는 정량적 목표 또는 비즈니스 모델 (예: "출시 6개월 내 DAU 10만 달성, 프리미엄 구독 전환율 5% 목표")

### 1. 페이지 개요

- **목적**: 이 페이지가 서비스 목표에 어떻게 기여하는지 포함하여 한 문장으로 서술
- **진입 경로**: 사용자가 어떤 경로로 이 페이지에 도달하는지 (예: GNB 클릭, 특정 버튼 클릭 등)
- **주요 사용자**: 이 페이지를 주로 사용하는 사용자 유형과 그 목적

### 2. 화면 레이아웃

ASCII 박스 문자(┌ ─ ┐ │ └ ┘ ├ ┤ ┬ ┴ ┼)로 실제 화면 구조를 그리세요.
- 주요 UI 요소에 ①②③... 번호를 매기세요.
- 헤더/본문/푸터 영역을 구분하세요.
- 실제 서비스처럼 보이게 현실적으로 그리세요.

```
[ASCII 레이아웃]
```

### 3. 요소별 인터랙션 명세

각 번호 요소에 대해 가능한 모든 사용자 액션과 시스템 반응을 명세하세요.
형식: `- **[액션]**: [시스템 반응]` (예: `- **검색어 입력 후 엔터**: 결과 목록 실시간 갱신, 0건이면 "결과 없음" 안내 텍스트 표시`)

① [요소명]
   - **[액션]**: [시스템 반응]

② [요소명]
   ...

### 4. 사용자 흐름 (User Flow)

이 페이지에서 가장 중요한 핵심 시나리오 2가지를 단계별로 서술하세요.

**시나리오 1: [시나리오명]**
1. [단계]
2. [단계]
3. [단계]

**시나리오 2: [시나리오명]**
1. [단계]
2. [단계]

### 5. 비즈니스 규칙 및 제약 조건

이 페이지에서 반드시 지켜야 할 규칙을 테이블로 작성하세요.

| 규칙 | 조건 | 처리 방식 |
|-----|------|---------|
| (예: 비로그인 접근) | (예: 로그인 없이 페이지 진입 시) | (예: 로그인 모달 표시 후 리다이렉트) |

### 6. 표시 데이터 명세

화면에 표시되는 주요 데이터 항목을 테이블로 정리하세요.

| 항목명 | 데이터 타입 | 출처 | 표시 형식 | 비고 |
|-------|-----------|-----|---------|-----|
| (예: 이슈 제목) | (예: string) | (예: 크롤링 원문) | (예: 최대 2줄, 초과 시 말줄임) | (예: 필수) |

### 7. 예외 처리 및 엣지 케이스

| 상황 | 사용자에게 보여줄 메시지 또는 처리 방식 |
|-----|--------------------------------------|
| (예: 데이터 로딩 실패) | (예: "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요." + 재시도 버튼) |
| (예: 검색 결과 0건) | (예: "검색 결과가 없습니다." + 관련 추천 이슈 3개 표시) |
"""

    if DUMMY_MODE:
        return {"content": f"## {page_name}\n\n> DUMMY MODE — API KEY 없음"}

    model = genai.GenerativeModel(GEMINI_MODEL)
    generation_config = genai.types.GenerationConfig(temperature=0.7)

    try:
        response = await model.generate_content_async(prompt, generation_config=generation_config)
        content = response.text.strip() if response.text else f"## {page_name}\n\n생성 실패"
        return {"content": content}
    except Exception as e:
        logger.error(f"Plan generate error for {page_name}: {e}")
        raise HTTPException(status_code=500, detail=f"기획서 생성 실패: {str(e)[:100]}")
