import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from data import formats
from data.generator import TYPES
from local import backend
from store import meta

# =================================================
# DATASET CATALOG AND PREVIEWS (local stand-in for aws/catalog.py)
# =================================================

SAMPLE_ROWS = 10


# Columns and row count of every table in a SQLite file
def sqlite_details(path: str) -> list:
    """
    Describe every table in a SQLite file.

    Arguments:
        path (str): SQLite file

    Returns:
        list: (table name, [{"name", "type"}], row count) tuples
    """
    details = []
    with closing(sqlite3.connect(path)) as con:
        for table in backend.sqlite_tables(path):
            columns = [{"name": row[1], "type": row[2].lower()} for row in con.execute(f'PRAGMA table_info("{table}")')]
            count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            details.append((table, columns, count))
    return details


# Size, count, format and last change of a folder's part files
def folder_stats(folder: str) -> dict:
    """
    Summarize the part files in a dataset folder.

    Arguments:
        folder (str): dataset folder

    Returns:
        dict: files, size, updated (ISO time) and format
    """
    parts = sorted(name for name in os.listdir(folder) if name.startswith("part-"))
    paths = [os.path.join(folder, name) for name in parts]
    newest = max(os.path.getmtime(path) for path in paths)
    return {
        "files": len(parts),
        "size": sum(os.path.getsize(path) for path in paths),
        "updated": datetime.fromtimestamp(newest, timezone.utc).isoformat(),
        "format": parts[0].rsplit(".", 1)[1],
    }


# Decode the first part file in a folder
def folder_sample(folder: str, hive_csv: bool) -> dict:
    """
    Read the first rows of a dataset folder's first part file.

    Arguments:
        folder (str): dataset folder
        hive_csv (bool): files were written for Athena (see formats.encode)

    Returns:
        dict: {"columns", "rows", "types", "row_count": None}
    """
    parts = sorted(name for name in os.listdir(folder) if name.startswith("part-")) if os.path.isdir(folder) else []
    if not parts:
        return {"columns": [], "rows": [], "row_count": 0}
    with open(os.path.join(folder, parts[0]), "rb") as f:
        names, rows, types = formats.decode(parts[0].rsplit(".", 1)[1], f.read(), hive_csv, SAMPLE_ROWS)
    return {"columns": names, "rows": rows, "types": types, "row_count": None}


# List every local dataset
def catalog(dbs: list) -> dict:
    """
    List every local dataset with metadata. Same shape as aws/catalog.catalog.

    Arguments:
        dbs (list): available database records

    Returns:
        dict: {"datasets": [...], "errors": []}
    """
    datasets = []

    # Postgres / Redshift: one SQLite file per database, tables named "<schema>.<table>"
    for db in dbs:
        for name, columns, count in sqlite_details(os.path.join(backend.DATA_DIR, db["file"])):
            schema, _, table = name.partition(".")
            datasets.append(
                {
                    "source": db["engine"],
                    "name": table,
                    "location": f"{db['name']}/{name}",
                    "dest": {"database": db["name"], "schema": schema, "table": table},
                    "columns": columns,
                    "rows": count,
                }
            )

    # DynamoDB
    if os.path.exists(backend.DYNAMO_PATH):
        for name, columns, count in sqlite_details(backend.DYNAMO_PATH):
            datasets.append({"source": "dynamodb", "name": name, "location": name, "dest": {"table": name}, "columns": columns, "rows": count})

    # S3: every folder under mock-data/ holding part files
    for folder, _, files in os.walk(backend.S3_ROOT):
        if folder == backend.S3_ROOT.rstrip(os.sep) or not any(name.startswith("part-") for name in files):
            continue
        prefix = os.path.relpath(folder, backend.S3_ROOT).replace(os.sep, "/")
        datasets.append(
            {
                "source": "s3",
                "name": prefix,
                "location": folder,
                "dest": {"prefix": prefix},
                **folder_stats(folder),
            }
        )

    # Glue: catalog entries in the metadata store
    for entry in meta.scan("GLUE#"):
        stats = folder_stats(entry["folder"]) if os.path.isdir(entry["folder"]) else {}
        datasets.append(
            {
                "source": "glue",
                "name": entry["table"],
                "location": f"{backend.GLUE_DATABASE}.{entry['table']} → {entry['folder']}",
                "dest": {"table": entry["table"]},
                "columns": [{"name": c["name"], "type": TYPES[c["type"]]["glue"]} for c in entry["columns"]],
                **stats,
                "format": entry["format"],
            }
        )

    return {"datasets": datasets, "errors": []}


# First rows of a local dataset
def sample(dest: dict) -> dict:
    """
    Read the first rows of a dataset. SQLite-backed sources also return the row count.

    Arguments:
        dest (dict): validated destination with a table

    Returns:
        dict: {"columns": [names], "rows": [dicts], "row_count": int or None, "types": {name: type} for Parquet/Avro files}
    """
    source = dest["source"]

    # SQLite-backed sources
    if source in backend.ENGINES or source == "dynamodb":
        path = backend.DYNAMO_PATH if source == "dynamodb" else os.path.join(backend.DATA_DIR, dest["db"]["file"])
        name = dest["table"] if source == "dynamodb" else f"{dest['schema']}.{dest['table']}"
        if name not in backend.sqlite_tables(path):
            raise ValueError(f"Table {name} doesn't exist")
        with closing(sqlite3.connect(path)) as con:
            cursor = con.execute(f'SELECT * FROM "{name}" LIMIT {SAMPLE_ROWS}')
            names = [column[0] for column in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return {"columns": names, "rows": rows, "row_count": count}

    # S3: the dataset folder
    if source == "s3":
        return folder_sample(backend.dataset_folder(dest), False)

    # Glue: the folder its catalog entry points at
    entry = meta.get(f"GLUE#{dest['table']}")
    if entry is None:
        raise ValueError(f"Glue table {backend.GLUE_DATABASE}.{dest['table']} doesn't exist")
    return folder_sample(entry["folder"], True)
