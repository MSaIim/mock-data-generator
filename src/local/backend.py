import os
import shutil
import sqlite3
import time
from contextlib import closing

from data import formats
from data.generator import TYPES
from store import meta

# =================================================
# LOCAL STAND-IN FOR aws/databases.py AND aws/writers.py
# Same function names, but everything lives under MOCKGEN_LOCAL_DIR and nothing touches AWS:
#   postgres / redshift databases -> one SQLite file each, tables named "<schema>.<table>"
#   dynamodb tables               -> dynamodb.sqlite
#   s3 buckets                    -> folders of part files in the chosen format
#   glue tables                   -> part files in a bucket folder plus a catalog entry in the metadata store
# =================================================

ENGINES = ("postgres", "redshift")

DATA_DIR = os.path.abspath(os.environ["MOCKGEN_LOCAL_DIR"])
S3_DIR = os.path.join(DATA_DIR, "s3")
DYNAMO_PATH = os.path.join(DATA_DIR, "dynamodb.sqlite")
os.makedirs(S3_DIR, exist_ok=True)

# =================================================
# HELPERS
# =================================================


# Folder of a dataset on disk
def dataset_folder(dest: dict) -> str:
    """
    Build the local folder of a dataset: s3/<bucket>/<prefix>/<table>.

    Arguments:
        dest (dict): validated destination

    Returns:
        str: absolute folder path
    """
    parts = [S3_DIR, dest["bucket"]] + (dest["prefix"].split("/") if dest["prefix"] else []) + [dest["table"]]
    return os.path.join(*parts)


# Replace a dataset folder's part files
def write_dataset(dest: dict, columns: list, batches, hive_csv: bool) -> tuple:
    """
    Write one part file per batch, replacing the dataset's old part files.

    Arguments:
        dest (dict): validated destination
        columns (list): validated column dicts
        batches: iterable of row lists
        hive_csv (bool): write CSV for Athena (see formats.encode)

    Returns:
        tuple: (folder, number of part files)
    """
    folder = dataset_folder(dest)
    os.makedirs(folder, exist_ok=True)
    # Remove old part files only (nested datasets in subfolders stay)
    for name in os.listdir(folder):
        if name.startswith("part-"):
            os.remove(os.path.join(folder, name))
    parts = 0
    for rows in batches:
        with open(os.path.join(folder, f"part-{parts:05d}.{dest['format']}"), "wb") as f:
            f.write(formats.encode(dest["format"], columns, rows, hive_csv))
        parts += 1
    return folder, parts


# Replace a SQLite table with new rows
def write_sqlite(path: str, table: str, columns: list, batches) -> None:
    """
    Drop and recreate the table, then insert every batch, in one transaction.

    Arguments:
        path (str): SQLite file
        table (str): table name (already validated)
        columns (list): validated column dicts
        batches: iterable of row lists
    """
    ddl = ", ".join(f'"{c["name"]}" {TYPES[c["type"]]["sql"]}' for c in columns)
    names = ", ".join(f'"{c["name"]}"' for c in columns)
    marks = ", ".join("?" for _ in columns)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute(f'CREATE TABLE "{table}" ({ddl})')
        for rows in batches:
            con.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', [tuple(row[c["name"]] for c in columns) for row in rows])


# List table names in a SQLite file
def sqlite_tables(path: str) -> list:
    """
    List tables in a SQLite file.

    Arguments:
        path (str): SQLite file

    Returns:
        list: table names
    """
    if not os.path.exists(path):
        return []
    with closing(sqlite3.connect(path)) as con:
        return [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


# Database record from the metadata store
def get_record(name: str) -> dict:
    """
    Load a database record.

    Arguments:
        name (str): database name

    Returns:
        dict: the record
    """
    item = meta.get("DB#" + name)
    if item is None:
        raise ValueError(f"No database named {name}")
    return item


# The fields of a record that are safe to list
def public(record: dict) -> dict:
    """
    Strip internal fields from a record.

    Arguments:
        record (dict): database record

    Returns:
        dict: name, engine, status, error and creator
    """
    return {"name": record["name"], "engine": record["engine"], "status": record["status"], "error": "", "created_by": record["created_by"]}


# =================================================
# DATABASES (admin)
# The create -> refresh -> available flow matches AWS, it just finishes on the first refresh
# =================================================


# List every registered database
def list_databases() -> list:
    """
    List all databases.

    Returns:
        list: public database records, sorted by name
    """
    result = [public(item) for item in meta.scan("DB#")]
    result.sort(key=lambda r: r["name"])
    return result


# Register a new local database
def create_database(name: str, engine: str, created_by: str) -> dict:
    """
    Register a database. Like AWS it starts as "creating"; refresh_database makes it available.

    Arguments:
        name (str): validated database name
        engine (str): postgres or redshift
        created_by (str): admin username

    Returns:
        dict: public record
    """
    record = {
        "pk": "DB#" + name,
        "name": name,
        "engine": engine,
        "file": f"{engine}-{name}.sqlite",
        "status": "creating",
        "created_by": created_by,
        "created_at": int(time.time()),
    }
    if not meta.put_new(record):
        raise ValueError(f"A database named {name} already exists")
    return public(record)


# Finish creating or deleting a local database
def refresh_database(name: str) -> dict:
    """
    Move a creating database to available (creating its file), or finish a deletion.

    Arguments:
        name (str): database name

    Returns:
        dict: public record, plus "connection" when available
    """
    record = get_record(name)
    path = os.path.join(DATA_DIR, record["file"])

    # Finish a deletion
    if record["status"] == "deleting":
        if os.path.exists(path):
            try:
                os.remove(path)
            except PermissionError:
                raise ValueError(f"{path} is open in another program. Close it and refresh again")
        meta.delete(record["pk"])
        return {**public(record), "status": "deleted"}

    # Finish creation
    sqlite3.connect(path).close()
    record["status"] = "available"
    meta.put(record)
    return {**public(record), "connection": connection_info(record)}


# Start deleting a local database
def delete_database(name: str) -> dict:
    """
    Mark a database as deleting. refresh_database removes its file.

    Arguments:
        name (str): database name

    Returns:
        dict: public record
    """
    record = get_record(name)
    record["status"] = "deleting"
    meta.put(record)
    return public(record)


# Load a database that is ready for use
def get_database(name: str, engine: str) -> dict:
    """
    Load a database that is ready for use.

    Arguments:
        name (str): database name
        engine (str): expected engine

    Returns:
        dict: the record
    """
    record = get_record(name)
    if record["engine"] != engine:
        raise ValueError(f"{name} is not a {engine} database")
    if record["status"] != "available":
        raise ValueError(f"{name} is not ready yet")
    return record


# How to open a local database
def connection_info(db: dict) -> dict:
    """
    Build the connection details for a local database.

    Arguments:
        db (dict): database record

    Returns:
        dict: type, file path and a sqlite3 command
    """
    path = os.path.join(DATA_DIR, db["file"])
    return {"type": f"{db['engine']} (local SQLite)", "file": path, "sqlite3": f'sqlite3 "{path}"'}


# =================================================
# WRITE
# =================================================


# Write generated batches to a local destination
def write(dest: dict, columns: list, batches) -> dict:
    """
    Create the destination if needed and write every batch.

    Arguments:
        dest (dict): validated destination (see app.check_destination)
        columns (list): validated column dicts
        batches: iterable of row lists

    Returns:
        dict: where the data went. For SQL sources the caller adds connection details
    """
    source = dest["source"]

    # Postgres / Redshift: schemas are folded into the table name
    if source in ENGINES:
        name = f"{dest['schema']}.{dest['table']}"
        write_sqlite(os.path.join(DATA_DIR, dest["db"]["file"]), name, columns, batches)
        return {"table": name}

    # DynamoDB
    if source == "dynamodb":
        write_sqlite(DYNAMO_PATH, dest["table"], columns, batches)
        return {"type": "dynamodb (local SQLite)", "file": DYNAMO_PATH, "table": dest["table"]}

    # S3: part files in the chosen format
    if source == "s3":
        folder, parts = write_dataset(dest, columns, batches, False)
        return {"type": "s3 (local folder)", "folder": folder, "format": dest["format"], "part_files": parts}

    # Glue: part files plus a catalog entry
    folder, parts = write_dataset(dest, columns, batches, True)
    meta.put(
        {
            "pk": f"GLUE#{dest['database']}#{dest['table']}",
            "database": dest["database"],
            "table": dest["table"],
            "folder": folder,
            "format": dest["format"],
            "columns": columns,
        }
    )
    return {
        "type": "glue (local catalog)",
        "database": dest["database"],
        "table": dest["table"],
        "folder": folder,
        "format": dest["format"],
        "part_files": parts,
    }


# =================================================
# BROWSING
# =================================================


# List local bucket folders
def list_buckets() -> list:
    """
    List bucket folders.

    Returns:
        list: bucket names
    """
    return sorted(name for name in os.listdir(S3_DIR) if os.path.isdir(os.path.join(S3_DIR, name)))


# List local Glue databases (those that have at least one table)
def list_glue_databases() -> list:
    """
    List Glue databases from the local catalog.

    Returns:
        list: database names
    """
    names = set()
    for item in meta.scan("GLUE#"):
        names.add(item["database"])
    return sorted(names)


# What already exists at a location, for the dropdowns
def browse(dest: dict) -> dict:
    """
    List existing schemas/tables/datasets at a partial destination. Same shapes as writers.browse.

    Arguments:
        dest (dict): validated destination without a table

    Returns:
        dict: {"schemas": {...}}, {"datasets": {...}} or {"tables": [...]}
    """
    source = dest["source"]

    # Postgres / Redshift: split "<schema>.<table>" names
    if source in ENGINES:
        schemas = {}
        for name in sqlite_tables(os.path.join(DATA_DIR, dest["db"]["file"])):
            schema, _, table = name.partition(".")
            schemas.setdefault(schema, []).append(table)
        return {"schemas": schemas}

    if source == "dynamodb":
        return {"tables": sqlite_tables(DYNAMO_PATH)}

    # S3: any folder holding part files is a dataset; its parent path is the prefix
    if source == "s3":
        datasets = {}
        root = os.path.join(S3_DIR, dest["bucket"])
        for folder, _, files in os.walk(root):
            if any(name.startswith("part-") for name in files):
                rel = os.path.relpath(folder, root).replace(os.sep, "/")
                prefix, _, table = rel.rpartition("/")
                datasets.setdefault(prefix, []).append(table)
        return {"datasets": datasets}

    # Glue: catalog entries for the database
    return {"tables": [item["table"] for item in meta.scan(f"GLUE#{dest['database']}#")]}


# =================================================
# DELETING
# =================================================


# Delete a local table or dataset
def delete_table(dest: dict) -> None:
    """
    Delete a table, a dataset's part files, or a Glue table and its files.

    Arguments:
        dest (dict): validated destination with a table
    """
    source = dest["source"]
    if source in ENGINES or source == "dynamodb":
        path = DYNAMO_PATH if source == "dynamodb" else os.path.join(DATA_DIR, dest["db"]["file"])
        name = dest["table"] if source == "dynamodb" else f"{dest['schema']}.{dest['table']}"
        with closing(sqlite3.connect(path)) as con, con:
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
        return
    if source == "s3":
        folder = dataset_folder(dest)
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.startswith("part-"):
                    os.remove(os.path.join(folder, name))
        return
    # Glue: remove the files the catalog entry points at, then the entry
    pk = f"GLUE#{dest['database']}#{dest['table']}"
    entry = meta.get(pk)
    if entry and os.path.isdir(entry["folder"]):
        for name in os.listdir(entry["folder"]):
            if name.startswith("part-"):
                os.remove(os.path.join(entry["folder"], name))
    meta.delete(pk)


# Delete a local bucket folder
def delete_bucket(bucket_name: str) -> None:
    """
    Delete a bucket folder and its files.

    Arguments:
        bucket_name (str): validated bucket name
    """
    shutil.rmtree(os.path.join(S3_DIR, bucket_name), ignore_errors=True)
