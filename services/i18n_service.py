from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from repositories.users_repo import UsersRepo


ROOT_DIR = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT_DIR / "content" / "i18n"
DEFAULT_LOCALE = "pt-BR"
SUPPORTED_LOCALES = {
    "pt-BR": "Português",
    "en-US": "English",
}


class I18nService:
    def __init__(self, users_repo: UsersRepo | None = None, i18n_dir: Path = I18N_DIR) -> None:
        self.users_repo = users_repo or UsersRepo()
        self.i18n_dir = i18n_dir
        self._cache: dict[str, dict[str, str]] = {}

    async def get_user_locale(self, user_id: int) -> str:
        user = await self.users_repo.get_user(user_id)
        locale = str(user.get("locale") or DEFAULT_LOCALE)
        return locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE

    async def set_user_locale(self, user_id: int, locale: str) -> None:
        if locale not in SUPPORTED_LOCALES:
            raise ValueError("unsupported_locale")
        await self.users_repo.set_locale(user_id, locale)

    async def t_user(self, user_id: int, key: str, **kwargs: Any) -> str:
        locale = await self.get_user_locale(user_id)
        return self.t(locale, key, **kwargs)

    def t(self, locale: str, key: str, **kwargs: Any) -> str:
        messages = self._load(locale)
        fallback = self._load(DEFAULT_LOCALE)
        template = messages.get(key) or fallback.get(key) or key
        return template.format(**kwargs)

    def maybe_t(self, locale: str, key: str, **kwargs: Any) -> str | None:
        messages = self._load(locale)
        fallback = self._load(DEFAULT_LOCALE)
        template = messages.get(key) or fallback.get(key)
        return template.format(**kwargs) if template else None

    def label(self, locale: str) -> str:
        return SUPPORTED_LOCALES.get(locale, SUPPORTED_LOCALES[DEFAULT_LOCALE])

    def _load(self, locale: str) -> dict[str, str]:
        if locale not in self._cache:
            path = self.i18n_dir / f"{locale}.json"
            with path.open("r", encoding="utf-8") as file:
                self._cache[locale] = json.load(file)
        return self._cache[locale]
