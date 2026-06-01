"""PostgreSQL-backed CMS config store (versioned, JSON-document pattern)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .enums import ConfigKind
from .models import ConfigKey, ConfigRecord, ConfigVersion
from .persistence import ConfigStore


class CmsCurrentRow(Base):
    __tablename__ = "config_current"
    __table_args__ = {"schema": "cms"}

    identity: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column()


class CmsVersionRow(Base):
    __tablename__ = "config_versions"
    __table_args__ = {"schema": "cms"}

    identity: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresConfigStore(ConfigStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_current(self, key: ConfigKey) -> ConfigRecord | None:
        async with self._db.session() as session:
            row = await session.get(CmsCurrentRow, key.identity())
            return ConfigRecord.model_validate(row.data) if row else None

    async def save_new_version(
        self,
        key: ConfigKey,
        value: dict[str, Any],
        *,
        comment: str | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        ident = key.identity()
        async with self._db.session() as session:
            current = await session.get(CmsCurrentRow, ident)
            last = (
                await session.execute(
                    select(func.max(CmsVersionRow.version)).where(
                        CmsVersionRow.identity == ident
                    )
                )
            ).scalar_one_or_none()
            version = (last or 0) + 1
            from app.shared.workflow.utils import utcnow

            now = utcnow()
            created_at = (
                ConfigRecord.model_validate(current.data).created_at if current else now
            )
            record = ConfigRecord(
                kind=key.kind,
                name=key.name,
                channel=key.channel,
                locale=key.locale,
                version=version,
                value=value,
                comment=comment,
                updated_by=updated_by,
                created_at=created_at,
                updated_at=now,
            )
            record_data = record.model_dump(mode="json")
            if current is None:
                session.add(
                    CmsCurrentRow(
                        identity=ident,
                        kind=str(key.kind),
                        name=key.name,
                        channel=str(key.channel) if key.channel else None,
                        locale=str(key.locale) if key.locale else None,
                        version=version,
                        data=record_data,
                    )
                )
            else:
                current.version = version
                current.data = record_data
            session.add(
                CmsVersionRow(
                    identity=ident,
                    version=version,
                    data=ConfigVersion(
                        kind=key.kind,
                        name=key.name,
                        channel=key.channel,
                        locale=key.locale,
                        version=version,
                        value=value,
                        comment=comment,
                        updated_by=updated_by,
                        created_at=now,
                    ).model_dump(mode="json"),
                )
            )
            return record

    async def list_versions(self, key: ConfigKey) -> list[ConfigVersion]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(CmsVersionRow)
                    .where(CmsVersionRow.identity == key.identity())
                    .order_by(CmsVersionRow.version)
                )
            ).scalars().all()
            return [ConfigVersion.model_validate(r.data) for r in rows]

    async def get_version(self, key: ConfigKey, version: int) -> ConfigVersion | None:
        async with self._db.session() as session:
            row = await session.get(CmsVersionRow, (key.identity(), version))
            return ConfigVersion.model_validate(row.data) if row else None

    async def delete_current(self, key: ConfigKey) -> bool:
        async with self._db.session() as session:
            row = await session.get(CmsCurrentRow, key.identity())
            if row is None:
                return False
            await session.delete(row)
            return True

    async def list_keys(self, kind: ConfigKind | None = None) -> list[ConfigKey]:
        stmt = select(CmsCurrentRow)
        if kind is not None:
            stmt = stmt.where(CmsCurrentRow.kind == str(kind))
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [
                ConfigKey(
                    kind=ConfigKind(r.kind),
                    name=r.name,
                    channel=Channel(r.channel) if r.channel else None,
                    locale=Locale(r.locale) if r.locale else None,
                )
                for r in rows
            ]
