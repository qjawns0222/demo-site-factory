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
    9: "데모 시나리오 및 시연 큐시트",
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

    return {"steps": steps}

@app.get("/api/session/{session_id}/steps")
async def get_session_steps(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    # 모든 step content를 Redis에서 한 번에 가져오기
    pipe = redis_client.pipeline()
    for step_id in range(1, 10):
        pipe.get(f"session:{session_id}:step:{step_id}")
    results = await pipe.execute()

    steps = {}
    for i, content in enumerate(results, start=1):
        if content:
            steps[i] = content

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

    summary_prompt = f"""
    아래는 사용자가 방금 승인한 {step_id}단계의 데이터입니다.
    이를 바탕으로 다음 단계를 위한 시스템 컨텍스트를 요약해 주세요.
    반드시 다음 형식의 순수 JSON으로 반환하세요: {{"summary": "...", "context": "..."}}
    내용: {sanitized_content}
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

    return {"status": "loop_closed", "summary": summary_result}

@app.get("/api/stream_step/{session_id}/{step_id}")
async def stream_step(request: Request, session_id: str, step_id: int, mode: str = "doc"):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="유효하지 않은 세션입니다. 해킹된 접근일 수 있습니다.")

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        try:
            db_prompt = ""
            # 코드 모드면 step_id에 100을 더한 ID로 조회 (코드 모드 프롬프트는 step_id+100으로 저장)
            prompt_step_id = step_id + 100 if mode == "code" else step_id
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT content FROM prompts WHERE step_id = ?", (prompt_step_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        db_prompt = row[0]
                # 코드 모드 프롬프트 없으면 일반 프롬프트로 fallback
                if not db_prompt and mode == "code":
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
                summaries = [f"[{h['step_id']}단계] {h.get('summary', '')}" for h in history]
                context_str = "이전 단계 누적 컨텍스트:\n" + "\n".join(summaries)

            mode_instruction = "\n\n[코드 생성 모드: 실제 구현 가능한 코드를 작성하세요. React/TypeScript, FastAPI/Python 등 구체적인 코드 포함.]" if mode == "code" else ""

            full_prompt = f"""
            [{domain}]에 대한 {'코드 생성' if mode == 'code' else '문서 생성'} 작업
            {context_str}

            {db_prompt}
            {mode_instruction}
            """

            generated_content = ""

            if DUMMY_MODE:
                dummy_tokens = f"DUMMY DATA STREAM: API 코드가 없습니다. 도메인 {domain}용 가상응답 {step_id} (모드: {mode})".split(" ")
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
            logger.error(f"Streaming error in step {step_id}: {err_str}")
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

        for key in keys:
            content = await redis_client.get(key)
            if not content: continue

            step_id_str = key.split(":")[-1]
            zf.writestr(f"step_{step_id_str}.md", content)

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

    memory_file.seek(0)
    safe_domain_filename = re.sub(r'[^a-zA-Z0-9_\-]', '_', domain)
    return StreamingResponse(
        memory_file,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={safe_domain_filename}_project.zip"}
    )

@app.get("/api/preview/{session_id}", response_class=HTMLResponse)
async def preview_html(session_id: str):
    domain = await redis_client.get(f"session_meta:{session_id}:domain")
    if not domain:
        raise HTTPException(status_code=404, detail="세션 만료이거나 유효하지 않은 UUID 접근입니다.")

    keys = await redis_scan_keys(f"session:{session_id}:step:*")
    if not keys:
        raise HTTPException(status_code=404, detail="프리뷰할 데이터가 없습니다. 먼저 Workflow를 시작하십시오.")

    # 단계 순서대로 수집 (100번대 code 모드 제외)
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

    # DUMMY MODE: Gemini 없이 간단한 HTML 반환
    if DUMMY_MODE:
        html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>{domain} — Demo</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px;}}
h1{{color:#333;}}p{{color:#666;}}</style></head>
<body><h1>{domain}</h1><p>더미 모드 — GEMINI_API_KEY를 설정하면 실제 데모가 생성됩니다.</p></body>
</html>"""
        return HTMLResponse(content=html)

    # Gemini로 동작하는 HTML 데모 생성
    prompt = f"""당신은 시니어 프론트엔드 개발자입니다.
아래는 "{domain}" 서비스의 기획/설계 문서입니다.

{spec_text}

위 문서를 바탕으로 완전히 동작하는 단일 HTML 파일 데모를 만들어주세요.

요구사항:
- 단일 HTML 파일 (CSS, JS 모두 인라인)
- 실제처럼 보이는 더미 하드코딩 데이터 포함
- 버튼 클릭, 탭 전환, 검색 등 핵심 인터랙션 동작
- 모던하고 깔끔한 UI (Tailwind CDN 사용 가능)
- 한국어 UI
- 실제 서비스처럼 자연스럽게 보일 것

반드시 완전한 HTML 파일만 출력하세요. 설명이나 마크다운 코드블록 없이 <!DOCTYPE html>부터 시작하세요."""

    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        generated = response.text.strip()

        # 코드블록 래핑 제거
        if generated.startswith("```html"):
            generated = generated[7:]
        if generated.startswith("```"):
            generated = generated[3:]
        if generated.endswith("```"):
            generated = generated[:-3]
        generated = generated.strip()

        return HTMLResponse(content=generated)
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
