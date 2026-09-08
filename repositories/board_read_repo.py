from __future__ import annotations

from pathlib import Path

import aiosqlite

from .db import DB_PATH


class BoardReadRepo:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    async def get_board_state(self, user_id: int) -> tuple[int, int]:
        await self._init_db()

        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT posicao_atual, voltas FROM players WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return 0, 0
            return int(row[0]), int(row[1])

    async def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER PRIMARY KEY,
                    posicao_atual INTEGER NOT NULL DEFAULT 0,
                    voltas INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.commit()
