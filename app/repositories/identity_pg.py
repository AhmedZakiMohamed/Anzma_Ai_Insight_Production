"""PostgreSQL adapter for canonical identity mappings; uses only the existing schema."""
from __future__ import annotations

from app.domain.identity import (AssetIdentity, BuildingIdentity, ExternalAssetMapping, SourceIdentity)


SQL_BUILDING = """
SELECT building_id, building_code
  FROM buildings
 WHERE building_code = %(building_code)s
"""

SQL_SOURCE = """
SELECT ds.source_id, ds.source_code, ds.building_id, b.building_code
  FROM data_sources ds
  JOIN buildings b USING (building_id)
 WHERE ds.source_code = %(source_code)s
"""

SQL_ASSET_IN_BUILDING = """
SELECT a.asset_id, a.asset_code, a.name AS asset_name, st.code AS system_code,
       l.building_id, b.building_code
  FROM assets a
  JOIN system_types st USING (system_type_id)
  JOIN locations l USING (location_id)
  JOIN buildings b USING (building_id)
 WHERE a.asset_code = %(asset_code)s AND l.building_id = %(building_id)s
"""

SQL_EXTERNAL_MAPPING = """
SELECT x.source_id, ds.source_code, x.external_id, a.asset_id, a.asset_code,
       a.name AS asset_name, st.code AS system_code, l.building_id, b.building_code
  FROM asset_external_ids x
  JOIN data_sources ds USING (source_id)
  JOIN assets a USING (asset_id)
  JOIN system_types st USING (system_type_id)
  JOIN locations l USING (location_id)
  JOIN buildings b ON b.building_id = l.building_id
 WHERE x.source_id = %(source_id)s AND x.external_id = %(external_id)s
"""

SQL_INSERT_EXTERNAL_MAPPING = """
INSERT INTO asset_external_ids(source_id, external_id, asset_id)
VALUES (%(source_id)s, %(external_id)s, %(asset_id)s)
ON CONFLICT (source_id, external_id) DO NOTHING
RETURNING source_id
"""


class IdentityPostgresStore:
    """IdentityStore implementation over a caller-owned dict-row cursor.

    Transaction ownership stays with the caller. The adapter never commits, deletes, or updates an external mapping.
    The existing database trigger validates source/building agreement; the existing primary key scopes uniqueness to
    `(source_id, external_id)`.
    """

    def __init__(self, cursor):
        self._cur = cursor

    @staticmethod
    def _building(row):
        return BuildingIdentity(int(row["building_id"]), row["building_code"])

    @staticmethod
    def _source(row):
        return SourceIdentity(int(row["source_id"]), row["source_code"], int(row["building_id"]),
                              row["building_code"])

    @staticmethod
    def _asset(row):
        return AssetIdentity(int(row["asset_id"]), row["asset_code"], row["asset_name"], row["system_code"],
                             int(row["building_id"]), row["building_code"])

    @staticmethod
    def _mapping(row):
        return ExternalAssetMapping(int(row["source_id"]), row["source_code"], row["external_id"],
                                    int(row["asset_id"]), row["asset_code"], row["asset_name"],
                                    row["system_code"], int(row["building_id"]), row["building_code"])

    def get_building(self, building_code: str) -> BuildingIdentity | None:
        self._cur.execute(SQL_BUILDING, {"building_code": building_code})
        row = self._cur.fetchone()
        return self._building(row) if row else None

    def get_source(self, source_code: str) -> SourceIdentity | None:
        self._cur.execute(SQL_SOURCE, {"source_code": source_code})
        row = self._cur.fetchone()
        return self._source(row) if row else None

    def get_asset_in_building(self, asset_code: str, building_id: int) -> AssetIdentity | None:
        self._cur.execute(SQL_ASSET_IN_BUILDING, {"asset_code": asset_code, "building_id": building_id})
        row = self._cur.fetchone()
        return self._asset(row) if row else None

    def get_external_mapping(self, source_id: int, external_id: str) -> ExternalAssetMapping | None:
        self._cur.execute(SQL_EXTERNAL_MAPPING, {"source_id": source_id, "external_id": external_id})
        row = self._cur.fetchone()
        return self._mapping(row) if row else None

    def insert_external_mapping(self, source: SourceIdentity, external_id: str,
                                asset: AssetIdentity) -> tuple[ExternalAssetMapping, bool]:
        self._cur.execute(SQL_INSERT_EXTERNAL_MAPPING,
                          {"source_id": source.source_id, "external_id": external_id, "asset_id": asset.asset_id})
        inserted = self._cur.fetchone() is not None
        mapping = self.get_external_mapping(source.source_id, external_id)
        if mapping is None:  # Defensive: after a successful insert the row must be visible in this transaction.
            raise RuntimeError("external mapping insert did not produce a readable mapping")
        return mapping, inserted


__all__ = ["IdentityPostgresStore"]
