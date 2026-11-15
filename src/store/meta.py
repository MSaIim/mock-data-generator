import json
import os

# =================================================
# METADATA STORE: DynamoDB in AWS, SQLite when running locally
# Items are dicts with a "pk" key: USER#<name>, DB#<name>, CONFIG#<name>
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
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError

    TABLE = boto3.resource("dynamodb").Table(os.environ["META_TABLE"])


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
    return TABLE.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")


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
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
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
    TABLE.put_item(Item=item)


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
    TABLE.delete_item(Key={"pk": pk})


# List all items whose key starts with a prefix
def scan(prefix: str) -> list:
    """
    List items by key prefix.

    Arguments:
        prefix (str): key prefix, e.g. "USER#"

    Returns:
        list: matching items
    """
    if LOCAL:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            rows = con.execute("SELECT item FROM meta WHERE substr(pk, 1, ?) = ? ORDER BY pk", (len(prefix), prefix)).fetchall()
        return [json.loads(row[0]) for row in rows]

    items = []
    kwargs = {"FilterExpression": Attr("pk").begins_with(prefix)}
    while True:
        resp = TABLE.scan(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return items
