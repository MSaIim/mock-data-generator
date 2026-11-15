import json
import os

# =================================================
# METADATA STORE: S3 JSON objects in AWS, SQLite when running locally
# Items are dicts with a "pk" key: DB#<name> (databases), GLUE#<db>#<table> (local Glue catalog)
# In S3, item DB#<name> is the object dependencies/DB/<name>.json in the app bucket
# =================================================

LOCAL = os.environ["MOCKGEN_LOCAL"] == "true"

if LOCAL:
    import sqlite3
    from contextlib import closing

    DB_PATH = os.path.join(os.environ["MOCKGEN_LOCAL_DIR"], "meta.sqlite")
    with closing(sqlite3.connect(DB_PATH)) as _con, _con:
        _con.execute("CREATE TABLE IF NOT EXISTS meta (pk TEXT PRIMARY KEY, item TEXT NOT NULL)")
else:
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    BUCKET = os.environ["APP_BUCKET"]
    PREFIX = "dependencies/"


# S3 object key for an item key (or key prefix)
def object_key(pk: str) -> str:
    """
    Map an item key to its S3 object key, e.g. DB#analytics -> dependencies/DB/analytics.json.

    Arguments:
        pk (str): item key

    Returns:
        str: S3 object key
    """
    return PREFIX + pk.replace("#", "/") + ".json"


# Read one item
def get(pk: str):
    """
    Read an item by key.

    Arguments:
        pk (str): item key

    Returns:
        dict | None: the item, or None if missing
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            row = con.execute("SELECT item FROM meta WHERE pk = ?", (pk,)).fetchone()
        return json.loads(row[0]) if row else None
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=object_key(pk))["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


# Write an item only if its key doesn't exist yet
def put_new(item: dict) -> bool:
    """
    Insert an item unless one with the same key exists. Atomic in both stores.

    Arguments:
        item (dict): item with a "pk" key

    Returns:
        bool: True if written, False if the key already existed
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            cur = con.execute("INSERT OR IGNORE INTO meta (pk, item) VALUES (?, ?)", (item["pk"], json.dumps(item)))
        return cur.rowcount == 1
    # S3 conditional write: fails if the object already exists
    try:
        s3.put_object(Bucket=BUCKET, Key=object_key(item["pk"]), Body=json.dumps(item), IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "PreconditionFailed":
            return False
        raise


# Write an item, replacing any existing one
def put(item: dict) -> None:
    """
    Insert or replace an item.

    Arguments:
        item (dict): item with a "pk" key
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("INSERT OR REPLACE INTO meta (pk, item) VALUES (?, ?)", (item["pk"], json.dumps(item)))
        return
    s3.put_object(Bucket=BUCKET, Key=object_key(item["pk"]), Body=json.dumps(item))


# Delete an item
def delete(pk: str) -> None:
    """
    Delete an item by key (no error if missing).

    Arguments:
        pk (str): item key
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("DELETE FROM meta WHERE pk = ?", (pk,))
        return
    s3.delete_object(Bucket=BUCKET, Key=object_key(pk))


# List all items whose key starts with a prefix
def scan(prefix: str) -> list:
    """
    List items by key prefix.

    Arguments:
        prefix (str): key prefix, e.g. "DB#"

    Returns:
        list: matching items
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            rows = con.execute("SELECT item FROM meta WHERE substr(pk, 1, ?) = ? ORDER BY pk", (len(prefix), prefix)).fetchall()
        return [json.loads(row[0]) for row in rows]

    # List the objects under the prefix (without the .json suffix), then read each one
    items = []
    key_prefix = PREFIX + prefix.replace("#", "/")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=key_prefix):
        for obj in page.get("Contents", []):
            items.append(json.loads(s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()))

    return items
