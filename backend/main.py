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

    prompt = f"""당신은 10년 경력의 시니어 프론트엔드 개발자입니다. 실제 프로덕션 수준의 인터랙티브 데모를 만드는 것이 목표입니다.
{extra_req_block}
아래는 "{domain}" 서비스의 기획/설계 문서입니다:

{spec_text}

---

위 문서를 바탕으로 완전히 동작하는 단일 HTML 파일 데모를 만들어주세요.

## 필수 기술 요구사항

1. **단일 파일**: CSS, JavaScript 모두 인라인 (외부 CDN은 Tailwind, Font Awesome만 허용)
2. **한국어 UI**: 모든 텍스트, 버튼, 레이블 한국어

## 더미 데이터 요구사항 (가장 중요)

더미 데이터는 절대 빠질 수 없습니다. 다음 규칙을 반드시 지키세요:

- 도메인 "{domain}"에 어울리는 **현실적이고 구체적인** 더미 데이터를 **최소 12개** 하드코딩
- 단순히 "항목 1", "사용자 A" 같은 무의미한 데이터 금지
- 실제 서비스처럼 보이는 이름, 날짜, 금액, 상태값, 설명문 포함
- 데이터는 JavaScript 배열/객체로 정의하고 렌더링 함수로 화면에 표시
- **반드시 `DOMContentLoaded` 또는 `window.onload`에서 렌더링 함수를 즉시 호출**해 초기 화면에 데이터가 표시되게 할 것
- 예시 패턴:
  ```
  const items = [{{id:1, name:"김민준", ...}}, ...];
  function renderItems(list) {{ /* DOM에 카드/행 생성 */ }}
  document.addEventListener('DOMContentLoaded', () => {{ renderItems(items); }});
  ```
- 페이지 첫 로드 시 메인 콘텐츠 영역이 **절대 비어있으면 안 됨** — 더미 데이터가 즉시 보여야 함
- 예시 (trend-service라면): `{{id:1, title:"K-팝 해외 반응 폭발", category:"Pop Culture", sentiment:"긍정", views:48200, saved:312, date:"2026-04-03"}}`

## UI/UX 품질 기준 (반드시 준수)

### 모달/팝업 규칙
- 모달은 **반드시 기본값 닫힘 상태** (display:none 또는 hidden class)
- 닫기 버튼(×)은 **반드시 클릭 시 모달 닫힘** 동작
- 모달 배경(overlay) 클릭 시에도 닫힘
- `document.getElementById('modal').style.display = 'flex'` 같은 명시적 show/hide 패턴 사용

### 화면 전환 규칙
- 탭/메뉴/사이드바는 **반드시 실제 콘텐츠가 전환**되어야 함 (단순 색상 변경 금지)
- 각 화면(뷰)은 별도 섹션으로 구현하고 show/hide로 전환
- 최소 3개 이상의 구분되는 화면 또는 탭 뷰 구현

### 인터랙션 규칙
- 모든 버튼은 클릭 시 **즉각적이고 눈에 보이는 반응** (상태 변경, 모달 열림, 데이터 갱신 등)
- 폼 제출 버튼은 **유효성 검사 후 성공 메시지 표시** + 목록에 실제로 추가
- 검색/필터는 **더미 데이터를 실제로 필터링**해서 결과 갱신

### select/dropdown 규칙 (절대 위반 금지)
- `<select>` 또는 드롭다운을 사용할 때는 **반드시 실제 선택 가능한 `<option>` 항목을 최소 3개 이상** 하드코딩
- `<option value="">선택하세요</option>` 같은 placeholder만 있고 실제 선택지가 없는 select 금지
- 예: 카테고리 select라면 `<option value="food">음식</option><option value="tech">기술</option>` 등 실제 값 포함
- select 값에 의존하는 폼 제출 로직은 선택지가 없을 때 제출 불가가 되므로 반드시 선택지를 채울 것

### 모달 닫기 규칙 (절대 위반 금지)
- **모든 모달/팝업/다이얼로그에는 반드시 닫기 버튼(×)이 있어야 함**
- 닫기 버튼 예시: `<button onclick="closeModal()" class="...">×</button>`
- 모달 열기 함수와 닫기 함수를 항상 쌍으로 구현: `openModal()` / `closeModal()`
- 모달 외부 배경(overlay) 클릭 시에도 닫힘 동작 추가

### 상태 관리 규칙
- 각 인터랙티브 요소의 상태를 JavaScript 변수로 명시적 관리
- `let isModalOpen = false` 같은 상태 변수 패턴 사용
- 중복 이벤트 리스너 방지 (addEventListener는 한 번만)

### 디자인 규칙
- 다크/라이트 테마 중 하나를 일관되게 유지
- 호버 상태, 포커스 상태 명시적 스타일링
- 로딩 상태가 있는 버튼은 로딩 인디케이터 표시

## 구현해야 할 핵심 기능 (최소 5개)

서비스 문서를 분석하여 가장 중요한 기능 5개 이상을 선택하고, 각각 실제로 동작하게 구현하세요:
- 대시보드/목록 뷰 (더미 데이터 렌더링)
- 상세 보기 (클릭 시 모달 또는 패널 열림/닫힘)
- 생성/추가 기능 (폼 → 목록에 항목 추가)
- 필터링/검색 (실제 데이터 필터링)
- 상태 변경 (토글, 승인/거절 등)

## 코드 품질 체크리스트 (출력 전 반드시 자가 검증)

- [ ] 더미 데이터가 최소 12개 이상 있고 현실적인가?
- [ ] `DOMContentLoaded`에서 렌더링 함수를 즉시 호출해 초기 화면이 비어있지 않은가?
- [ ] 모든 모달이 초기에 **닫힌 상태** (display:none)인가?
- [ ] 모든 닫기(×) 버튼에 `onclick` 핸들러가 **반드시 붙어있는가**?
- [ ] 모든 탭/메뉴 클릭이 **실제 콘텐츠를 show/hide** 전환하는가?
- [ ] 모든 "등록", "저장", "추가" 버튼이 클릭 시 **목록에 실제로 항목이 추가**되는가?
- [ ] 검색/필터 입력 시 **더미 데이터가 실시간 필터링**되는가?
- [ ] console.error가 발생할 null 참조 (`getElementById` 결과 없음 등)가 없는가?
- [ ] 이벤트 리스너 중복 등록이 없는가?
- [ ] **모든 버튼이 클릭 시 즉각적이고 눈에 보이는 반응**을 하는가?
- [ ] 모든 `<select>`에 실제 선택 가능한 `<option>`이 최소 3개 이상 있는가?
- [ ] 모든 모달/팝업에 닫기(×) 버튼이 있고 `onclick`으로 실제 닫히는가?

## 출력 형식

반드시 완전한 HTML 파일만 출력하세요.
설명, 주석 블록, 마크다운 코드블록(```) 없이 정확히 <!DOCTYPE html>로 시작하고 </html>로 끝내세요."""

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
        logger.error(f"[Preview] Background generation failed for {session_id}: {e}")
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
