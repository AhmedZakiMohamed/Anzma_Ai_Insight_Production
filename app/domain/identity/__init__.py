"""Canonical, source-scoped identities used by ingestion and access-control analytics.

This first version covers buildings, sources, assets, and door aliases. Person identity is deliberately out of scope:
the current schema has no person identity table and no personnel authorization model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IdentityNotFound(ValueError):
    """A requested building, source, or in-scope asset does not exist."""


class IdentityConflict(ValueError):
    """An external ID already has a different canonical mapping."""


class InvalidExternalId(ValueError):
    """An external ID is empty or outside the existing database length constraint."""


@dataclass(frozen=True)
class BuildingIdentity:
    building_id: int
    building_code: str


@dataclass(frozen=True)
class SourceIdentity:
    source_id: int
    source_code: str
    building_id: int
    building_code: str


@dataclass(frozen=True)
class AssetIdentity:
    asset_id: int
    asset_code: str
    asset_name: str
    system_code: str
    building_id: int
    building_code: str


@dataclass(frozen=True)
class ExternalAssetMapping:
    source_id: int
    source_code: str
    external_id: str
    asset_id: int
    asset_code: str
    asset_name: str
    system_code: str
    building_id: int
    building_code: str


@dataclass(frozen=True)
class MappingResult:
    mapping: ExternalAssetMapping
    created: bool


class IdentityStore(Protocol):
    """Persistence operations required by the identity service; implementations must not reassign mappings."""

    def get_building(self, building_code: str) -> BuildingIdentity | None: ...
    def get_source(self, source_code: str) -> SourceIdentity | None: ...
    def get_asset_in_building(self, asset_code: str, building_id: int) -> AssetIdentity | None: ...
    def get_external_mapping(self, source_id: int, external_id: str) -> ExternalAssetMapping | None: ...
    def insert_external_mapping(self, source: SourceIdentity, external_id: str,
                                asset: AssetIdentity) -> tuple[ExternalAssetMapping, bool]: ...


class IdentityMappings:
    """Application policy for immutable, source-scoped external asset and door IDs.

    Mapping means adding an alias. Repeating the same mapping is idempotent; assigning that external ID to a
    different canonical asset is rejected. The database primary key and building trigger provide uniqueness and
    same-building protection; immutability is enforced by this write interface (no UPDATE is issued).
    """

    def __init__(self, store: IdentityStore):
        self._store = store

    def building(self, building_code: str) -> BuildingIdentity:
        found = self._store.get_building(building_code)
        if found is None:
            raise IdentityNotFound(f"Unknown building code {building_code!r}.")
        return found

    def source(self, source_code: str) -> SourceIdentity:
        found = self._store.get_source(source_code)
        if found is None:
            raise IdentityNotFound("Unknown source code.")
        return found

    def map_asset(self, *, source_code: str, external_id: str, asset_code: str) -> MappingResult:
        return self._map(source_code=source_code, external_id=external_id, asset_code=asset_code)

    def map_door(self, *, source_code: str, external_id: str, asset_code: str) -> MappingResult:
        """Map an external door/controller ID to an ACCESS_CONTROL canonical asset alias."""
        return self._map(source_code=source_code, external_id=external_id, asset_code=asset_code,
                         required_system_code="ACCESS_CONTROL")

    def resolve_asset(self, *, source_code: str, external_id: str) -> ExternalAssetMapping | None:
        source = self.source(source_code)
        return self._store.get_external_mapping(source.source_id, external_id)

    def resolve_door(self, *, source_code: str, external_id: str) -> ExternalAssetMapping | None:
        mapping = self.resolve_asset(source_code=source_code, external_id=external_id)
        return mapping if mapping is None or mapping.system_code == "ACCESS_CONTROL" else None

    def _map(self, *, source_code: str, external_id: str, asset_code: str,
             required_system_code: str | None = None) -> MappingResult:
        if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 128:
            raise InvalidExternalId("external_id must contain 1 through 128 non-whitespace characters.")
        source = self.source(source_code)
        asset = self._store.get_asset_in_building(asset_code, source.building_id)
        if asset is None:
            raise IdentityNotFound("Unknown asset code in the source's building.")
        if required_system_code and asset.system_code != required_system_code:
            raise IdentityConflict(f"Door mapping requires a {required_system_code} asset.")

        current = self._store.get_external_mapping(source.source_id, external_id)
        if current is not None:
            if current.asset_id != asset.asset_id:
                raise IdentityConflict("External ID is already mapped to a different asset; mapping was not changed.")
            return MappingResult(current, created=False)

        # The store uses INSERT .. ON CONFLICT DO NOTHING and reads the winner back, covering concurrent attempts.
        mapping, created = self._store.insert_external_mapping(source, external_id, asset)
        if mapping.asset_id != asset.asset_id:
            raise IdentityConflict("External ID is already mapped to a different asset; mapping was not changed.")
        return MappingResult(mapping, created=created)


__all__ = ["AssetIdentity", "BuildingIdentity", "ExternalAssetMapping", "IdentityConflict", "IdentityMappings",
           "IdentityNotFound", "IdentityStore", "InvalidExternalId", "MappingResult", "SourceIdentity"]
