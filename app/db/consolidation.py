"""One-time consolidation of the legacy ``Adira`` database into ``resume_ats``.

The two databases live on the same MongoDB server.  Every collection is
copied, including GridFS ``.files`` and ``.chunks`` collections.  Documents
with the same ``_id`` are refreshed from the legacy database; documents that
collide on another unique key (most commonly the seeded administrator email)
are merged into the canonical row while retaining its ``_id``.
"""
from __future__ import annotations

from typing import Any

from pymongo.errors import DuplicateKeyError, OperationFailure


# MongoDB database names are case-sensitive.  The legacy production database
# is named exactly ``Adira`` (capital A), as shown by the server's database
# browser.  Using lowercase here silently addressed a different, empty
# database and made a no-op migration look successful.
LEGACY_DATABASE = "Adira"
CANONICAL_DATABASE = "resume_ats"
_MISSING = object()


def _nested_value(document: dict, dotted_key: str):
    value: Any = document
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _unique_match(collection, document: dict):
    """Find a target row representing ``document`` through a unique index."""
    for index in collection.index_information().values():
        if not index.get("unique") or index.get("name") == "_id_":
            continue
        query = {}
        for key, _direction in index.get("key", []):
            value = _nested_value(document, key)
            if value is _MISSING or (index.get("sparse") and value is None):
                query = {}
                break
            query[key] = value
        if query:
            matched = collection.find_one(query, {"_id": 1})
            if matched:
                return matched
    return None


def _copy_indexes(source, target) -> tuple[int, list[str]]:
    created = 0
    errors: list[str] = []
    for index in source.index_information().values():
        if index.get("name") == "_id_":
            continue
        options = {
            key: value
            for key, value in index.items()
            if key not in {"v", "key", "ns"}
        }
        try:
            target.create_index(index["key"], **options)
            created += 1
        except OperationFailure as exc:
            # The application creates its own equivalent indexes at startup;
            # a name/options conflict does not mean any document was lost.
            errors.append(f"{index.get('name')}: {exc.code or 'operation failure'}")
    return created, errors


def consolidate_adira_into_resume_ats(client, *, drop_legacy: bool = False) -> dict:
    """Merge every legacy collection and optionally drop it after verification."""
    if LEGACY_DATABASE == CANONICAL_DATABASE:  # defensive guard around drop_database
        raise RuntimeError("Legacy and canonical MongoDB databases must be different")

    # Do not let a capitalization typo (or a repeated request after completion)
    # masquerade as a successful zero-document migration.
    if LEGACY_DATABASE not in client.list_database_names():
        return {
            "source_database": LEGACY_DATABASE,
            "target_database": CANONICAL_DATABASE,
            "source_found": False,
            "source_documents": 0,
            "missing_documents": 0,
            "verified": False,
            "legacy_dropped": False,
            "collections": {},
        }

    source = client[LEGACY_DATABASE]
    target = client[CANONICAL_DATABASE]
    collection_reports: dict[str, dict] = {}
    total_source = 0
    total_missing = 0

    for name in source.list_collection_names():
        source_collection = source[name]
        target_collection = target[name]
        source_documents = list(source_collection.find({}))
        total_source += len(source_documents)
        report = {
            "source": len(source_documents),
            "inserted": 0,
            "replaced_by_id": 0,
            "merged_by_unique_key": 0,
            "verified": 0,
            "missing": 0,
            "indexes_created": 0,
            "index_warnings": [],
        }

        for document in source_documents:
            if target_collection.find_one({"_id": document["_id"]}, {"_id": 1}):
                target_collection.replace_one({"_id": document["_id"]}, document)
                report["replaced_by_id"] += 1
                continue
            try:
                target_collection.insert_one(document)
                report["inserted"] += 1
            except DuplicateKeyError:
                matched = _unique_match(target_collection, document)
                if not matched:
                    raise
                updates = {key: value for key, value in document.items() if key != "_id"}
                if updates:
                    target_collection.update_one({"_id": matched["_id"]}, {"$set": updates})
                report["merged_by_unique_key"] += 1

        created, warnings = _copy_indexes(source_collection, target_collection)
        report["indexes_created"] = created
        report["index_warnings"] = warnings

        for document in source_documents:
            present = target_collection.find_one({"_id": document["_id"]}, {"_id": 1})
            if not present:
                present = _unique_match(target_collection, document)
            if present:
                report["verified"] += 1
            else:
                report["missing"] += 1

        total_missing += report["missing"]
        collection_reports[name] = report

    verified = total_missing == 0
    dropped = False
    if drop_legacy and verified:
        client.drop_database(LEGACY_DATABASE)
        dropped = LEGACY_DATABASE not in client.list_database_names()

    return {
        "source_database": LEGACY_DATABASE,
        "target_database": CANONICAL_DATABASE,
        "source_found": True,
        "source_documents": total_source,
        "missing_documents": total_missing,
        "verified": verified,
        "legacy_dropped": dropped,
        "collections": collection_reports,
    }
