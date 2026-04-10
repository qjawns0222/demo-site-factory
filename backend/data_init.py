import os
import aiosqlite
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "prompts.db")
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                step_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL
            )
        """)

        # 항상 파일 시스템과 DB를 동기화 (INSERT OR REPLACE로 업그레이드 시에도 반영)
        if os.path.exists(PROMPTS_DIR):
            import aiofiles
            seeded = 0
            seeded_ids = set()
            for file_name in sorted(os.listdir(PROMPTS_DIR)):
                # 일반 프롬프트: step_N.txt → step_id = N
                # 코드 모드 프롬프트: step_N_code.txt → step_id = N + 100
                step_id = None
                if file_name.startswith("step_") and file_name.endswith(".txt"):
                    base = file_name.replace("step_", "").replace(".txt", "")
                    if base.endswith("_code"):
                        num = base.replace("_code", "")
                        if num.isdigit():
                            step_id = int(num) + 100
                    elif base.isdigit():
                        step_id = int(base)

                if step_id is not None:
                    file_path = os.path.join(PROMPTS_DIR, file_name)
                    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                        content = await f.read()
                    await db.execute(
                        "INSERT OR REPLACE INTO prompts (step_id, content) VALUES (?, ?)",
                        (step_id, content)
                    )
                    seeded_ids.add(step_id)
                    seeded += 1

            # 파일에 없는 step_id는 DB에서 제거
            if seeded_ids:
                placeholders = ",".join("?" * len(seeded_ids))
                await db.execute(
                    f"DELETE FROM prompts WHERE step_id NOT IN ({placeholders})",
                    list(seeded_ids)
                )
            await db.commit()
            logger.info(f"Prompts synced to SQLite: {seeded} entries (INSERT OR REPLACE).")
        else:
            logger.warning("PROMPTS_DIR does not exist, cannot seed.")
