import base64
import json
import os
import re
import ssl
import time
import uuid
from decimal import Decimal

import boto3
import pg8000.native
from botocore.exceptions import ClientError

from data import formats
from data.generator import TYPES

# =================================================
# CONFIG AND CLIENTS
# =================================================

REGION = os.environ["AWS_REGION"]
APP_BUCKET = os.environ["APP_BUCKET"]
REDSHIFT_ROLE_ARN = os.environ["REDSHIFT_ROLE_ARN"]

# The app only writes under these prefixes of the app bucket (no other buckets)
STAGING_PREFIX = "staging/"
DATA_PREFIX = "mock-data/"
TABLES_PREFIX = "mock-tables/"
S3_ROOT = f"s3://{APP_BUCKET}/{DATA_PREFIX}"
GLUE_ROOT = f"s3://{APP_BUCKET}/{TABLES_PREFIX}"

# Every Glue table goes in this one database
GLUE_DATABASE = "mock_data"

# Tag on every DynamoDB table the app creates. Only tagged tables can be listed, replaced or deleted
APP_TAG = {"Key": "created-by", "Value": "mock-data-generator"}

# Size of each multi-row Postgres INSERT
PG_STATEMENT_CHARS = 1000000

# Dataset files look like <folder>/part-00000.<ext>; the folder (S3: the prefix) is the dataset
PART_RE = re.compile(r"^(.+)/part-\d{5}\.[a-z]+$")

# Every schema and its tables (Postgres and Redshift), excluding system schemas
SCHEMAS_SQL = (
    "SELECT n.nspname, c.relname FROM pg_namespace n "
    "LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind = 'r' "
    "WHERE substring(n.nspname, 1, 3) <> 'pg_' AND n.nspname <> 'information_schema' "
    "ORDER BY 1, 2"
)

# How Athena reads each file format
GLUE_FORMATS = {
    "csv": {
        "serde": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
        "params": {"field.delim": ",", "escape.delim": "\\", "serialization.null.format": ""},
        "input": "org.apache.hadoop.mapred.TextInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    },
    "json": {
        "serde": "org.openx.data.jsonserde.JsonSerDe",
        "params": {},
        "input": "org.apache.hadoop.mapred.TextInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    },
    "parquet": {
        "serde": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
        "params": {},
        "input": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    },
    "avro": {
        "serde": "org.apache.hadoop.hive.serde2.avro.AvroSerDe",
        "params": {},
        "input": "org.apache.hadoop.hive.ql.io.avro.AvroContainerInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.avro.AvroContainerOutputFormat",
    },
}

s3 = boto3.client("s3")
dynamo = boto3.client("dynamodb")
dynamo_resource = boto3.resource("dynamodb")
glue = boto3.client("glue")
redshift_data = boto3.client("redshift-data")

# =================================================
# SQL HELPERS
# =================================================


# Open a connection to an Aurora Postgres cluster
def pg_connect(db: dict) -> pg8000.native.Connection:
    """
    Connect to Postgres. A paused cluster (0 ACU) takes a few seconds to wake up,
    so this retries for about a minute.

    Arguments:
        db (dict): database record from databases.get_database

    Returns:
        pg8000.native.Connection: open connection
    """
    # Encrypted in transit. The RDS CA isn't in Lambda's trust store, so the certificate isn't verified (traffic stays in the VPC)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(6):
        try:
            return pg8000.native.Connection(
                user=db["username"],
                # Stored base64-encoded (see databases.create_database)
                password=base64.b64decode(db["password"]).decode(),
                host=db["host"],
                port=int(db["port"]),
                database=db["database"],
                ssl_context=ctx,
                timeout=30,
            )
        except (pg8000.native.InterfaceError, OSError):
            if attempt == 5:
                raise
            time.sleep(10)


# Run SQL statements on Postgres and return the rows of the last one
def pg_run(db: dict, sqls: list) -> list:
    """
    Open a connection, run the statements in order, and close it.

    Arguments:
        db (dict): database record
        sqls (list): statements to run

    Returns:
        list: rows returned by the last statement
    """
    con = pg_connect(db)
    try:
        rows = []
        for sql in sqls:
            rows = con.run(sql) or []
        return rows
    except pg8000.native.DatabaseError as e:
        raise ValueError(f"Postgres: {e}")
    finally:
        con.close()


# Run SQL on Redshift Serverless through the Data API and wait for it to finish
def rs_exec(db: dict, sqls: list) -> tuple:
    """
    Run one statement, or several as a single transaction, and wait for the result.
    Signs in as the Lambda's IAM role (no stored secret needed).

    Arguments:
        db (dict): database record
        sqls (list): statements to run

    Returns:
        tuple: (column names, rows as lists of plain values) for a single statement; ([], []) for batches
    """
    # Submit the statement(s)
    target = {"WorkgroupName": db["ident"], "Database": db["database"]}
    if len(sqls) == 1:
        statement_id = redshift_data.execute_statement(Sql=sqls[0], **target)["Id"]
    else:
        statement_id = redshift_data.batch_execute_statement(Sqls=sqls, **target)["Id"]

    # Poll until done
    while True:
        desc = redshift_data.describe_statement(Id=statement_id)
        if desc["Status"] == "FINISHED":
            break
        if desc["Status"] in ("FAILED", "ABORTED"):
            raise ValueError("Redshift: " + desc.get("Error", desc["Status"]))
        time.sleep(0.5)

    # Read results for single statements that return rows
    if len(sqls) > 1 or not desc.get("HasResultSet"):
        return [], []
    result = redshift_data.get_statement_result(Id=statement_id)
    names = [column["name"] for column in result["ColumnMetadata"]]
    rows = []
    # Each field is {"stringValue"|"longValue"|"doubleValue"|"booleanValue": value} or {"isNull": true}
    for record in result["Records"]:
        rows.append([None if field.get("isNull") else next(iter(field.values())) for field in record])
    return names, rows


# Turn a Python value into a SQL literal
def sql_literal(value) -> str:
    """
    Format a generated value as a SQL literal. Strings are escaped by doubling single quotes.

    Arguments:
        value: None, bool, int, float or str

    Returns:
        str: SQL literal
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


# SQL statements that (re)create a schema-qualified table
def recreate_sql(dest: dict, columns: list) -> list:
    """
    Build the statements that create the schema if missing and replace the table.
    Names are pre-validated, then quoted.

    Arguments:
        dest (dict): validated destination
        columns (list): validated column dicts

    Returns:
        list: SQL statements
    """
    ddl = ", ".join(f'"{c["name"]}" {TYPES[c["type"]]["sql"]}' for c in columns)
    qualified = f'"{dest["schema"]}"."{dest["table"]}"'
    return [
        f'CREATE SCHEMA IF NOT EXISTS "{dest["schema"]}"',
        f"DROP TABLE IF EXISTS {qualified}",
        f"CREATE TABLE {qualified} ({ddl})",
    ]


# =================================================
# S3 HELPERS
# =================================================


# Folder of a dataset inside the app bucket
def dataset_folder(dest: dict) -> str:
    """
    Build the key prefix of a dataset: mock-data/<prefix>/ for S3, mock-tables/<table>/ for Glue.

    Arguments:
        dest (dict): validated destination

    Returns:
        str: key prefix ending with /
    """
    if dest["source"] == "glue":
        return f"{TABLES_PREFIX}{dest['table']}/"
    return f"{DATA_PREFIX}{dest['prefix']}/"


# Delete every object under a prefix
def delete_prefix(bucket: str, prefix: str) -> None:
    """
    Delete all objects under a prefix.

    Arguments:
        bucket (str): bucket name
        prefix (str): key prefix
    """
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# Delete a dataset's part files (not nested datasets that share the folder name)
def delete_dataset(bucket: str, folder: str) -> None:
    """
    Delete the part files directly inside a dataset folder.

    Arguments:
        bucket (str): bucket name
        folder (str): dataset key prefix ending with /
    """
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=folder, Delimiter="/"):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# Write a dataset's part files, replacing the old ones
def write_dataset(dest: dict, columns: list, batches, hive_csv: bool) -> tuple:
    """
    Write one part file per batch to the dataset's folder in the app bucket.

    Arguments:
        dest (dict): validated destination
        columns (list): validated column dicts
        batches: iterable of row lists
        hive_csv (bool): write CSV for Athena (see formats.encode)

    Returns:
        tuple: (folder, number of part files)
    """
    # Replace old part files with one new file per batch
    folder = dataset_folder(dest)
    delete_dataset(APP_BUCKET, folder)
    parts = 0
    for rows in batches:
        body = formats.encode(dest["format"], columns, rows, hive_csv)
        s3.put_object(Bucket=APP_BUCKET, Key=f"{folder}part-{parts:05d}.{dest['format']}", Body=body)
        parts += 1
    return folder, parts


# =================================================
# DYNAMODB HELPERS
# =================================================


# Whether a DynamoDB table exists, and whether this app created it
def dynamo_owner(name: str) -> str:
    """
    Check who owns a DynamoDB table, by the app's tag.

    Arguments:
        name (str): table name

    Returns:
        str: "missing", "app" (created by this app) or "other"
    """
    try:
        arn = dynamo.describe_table(TableName=name)["Table"]["TableArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return "missing"
        raise
    return "app" if APP_TAG in dynamo.list_tags_of_resource(ResourceArn=arn).get("Tags", []) else "other"


# Refuse to touch a DynamoDB table this app didn't create
def require_app_table(name: str) -> None:
    """
    Raise unless the table exists and carries the app's tag.

    Arguments:
        name (str): table name
    """
    owner = dynamo_owner(name)
    if owner == "missing":
        raise ValueError(f"DynamoDB table {name} doesn't exist")
    if owner == "other":
        raise ValueError(f"DynamoDB table {name} wasn't created by this app, so it can't be used here")


# Every DynamoDB table this app created
def app_tables() -> list:
    """
    List the tagged DynamoDB tables with their descriptions. Tables the app isn't allowed to inspect
    (or that disappear while listing) are skipped: the app can't have created them.

    Returns:
        list: DescribeTable "Table" dicts
    """
    tables = []
    for page in dynamo.get_paginator("list_tables").paginate():
        for name in page["TableNames"]:
            try:
                table = dynamo.describe_table(TableName=name)["Table"]
                tags = dynamo.list_tags_of_resource(ResourceArn=table["TableArn"]).get("Tags", [])
            except ClientError as e:
                if e.response["Error"]["Code"] in ("AccessDeniedException", "ResourceNotFoundException"):
                    continue
                raise
            if APP_TAG in tags:
                tables.append(table)
    return tables


# =================================================
# WRITE
# =================================================


# Write generated batches to a destination, replacing the table if it exists
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

    # Postgres: one transaction, multi-row INSERTs per batch
    if source == "postgres":
        con = pg_connect(dest["db"])
        try:
            con.run("BEGIN")
            for sql in recreate_sql(dest, columns):
                con.run(sql)
            names = ", ".join(f'"{c["name"]}"' for c in columns)
            prefix = f'INSERT INTO "{dest["schema"]}"."{dest["table"]}" ({names}) VALUES '
            for rows in batches:
                chunk = []
                size = 0
                for row in rows:
                    values = "(" + ", ".join(sql_literal(row[c["name"]]) for c in columns) + ")"
                    if chunk and size + len(values) > PG_STATEMENT_CHARS:
                        con.run(prefix + ", ".join(chunk))
                        chunk = []
                        size = 0
                    chunk.append(values)
                    size += len(values) + 2
                if chunk:
                    con.run(prefix + ", ".join(chunk))
            con.run("COMMIT")
        except pg8000.native.DatabaseError as e:
            raise ValueError(f"Postgres: {e}")
        finally:
            con.close()
        return {"table": f"{dest['schema']}.{dest['table']}"}

    # Redshift: stage one JSON file per batch, then replace the table and COPY them all in one transaction
    if source == "redshift":
        staging = f"{STAGING_PREFIX}redshift/{uuid.uuid4()}/"
        try:
            for i, rows in enumerate(batches):
                body = "\n".join(json.dumps(row) for row in rows)
                s3.put_object(Bucket=APP_BUCKET, Key=f"{staging}part-{i:05d}.json", Body=body.encode("utf-8"))
            copy = (
                f'COPY "{dest["schema"]}"."{dest["table"]}" FROM \'s3://{APP_BUCKET}/{staging}\' '
                f"IAM_ROLE '{REDSHIFT_ROLE_ARN}' FORMAT AS JSON 'auto' DATEFORMAT 'auto' TIMEFORMAT 'auto'"
            )
            rs_exec(dest["db"], recreate_sql(dest, columns) + [copy])
        finally:
            delete_prefix(APP_BUCKET, staging)
        return {"table": f"{dest['schema']}.{dest['table']}"}

    # DynamoDB: recreate the table (only if this app made it), then write each batch. Every item gets a uuid "pk"
    if source == "dynamodb":
        name = dest["table"]
        wait = {"Delay": 2, "MaxAttempts": 90}
        owner = dynamo_owner(name)
        if owner == "other":
            raise ValueError(f"DynamoDB table {name} already exists and wasn't created by this app, so it can't be replaced. Pick another name")
        if owner == "app":
            dynamo.delete_table(TableName=name)
            dynamo.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig=wait)
        dynamo.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
            Tags=[APP_TAG],
        )
        dynamo.get_waiter("table_exists").wait(TableName=name, WaiterConfig=wait)
        table = dynamo_resource.Table(name)
        for rows in batches:
            with table.batch_writer() as writer:
                for row in rows:
                    item = {"pk": str(uuid.uuid4())}
                    # DynamoDB needs Decimal instead of float
                    for key, value in row.items():
                        item[key] = Decimal(str(value)) if isinstance(value, float) else value
                    writer.put_item(Item=item)
        arn = dynamo.describe_table(TableName=name)["Table"]["TableArn"]
        return {"type": "dynamodb", "table": name, "region": REGION, "arn": arn, "partition_key": "pk"}

    # S3: part files in the chosen format
    if source == "s3":
        folder, parts = write_dataset(dest, columns, batches, False)
        return {"type": "s3", "uri": f"s3://{APP_BUCKET}/{folder}", "format": dest["format"], "part_files": parts, "region": REGION}

    # Glue: part files under mock-tables/ plus a catalog table over them (Athena-readable)
    database = GLUE_DATABASE
    folder, parts = write_dataset(dest, columns, batches, True)
    location = f"s3://{APP_BUCKET}/{folder}"
    # Create the Glue database the first time it's needed
    try:
        glue.get_database(Name=database)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
        glue.create_database(DatabaseInput={"Name": database})
    try:
        glue.delete_table(DatabaseName=database, Name=dest["table"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    fmt = GLUE_FORMATS[dest["format"]]
    params = {"classification": dest["format"]}
    if dest["format"] == "csv":
        params["skip.header.line.count"] = "1"
    if dest["format"] == "avro":
        params["avro.schema.literal"] = json.dumps(formats.avro_schema(columns))
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": dest["table"],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": params,
            "StorageDescriptor": {
                "Columns": [{"Name": c["name"], "Type": TYPES[c["type"]]["glue"]} for c in columns],
                "Location": location,
                "InputFormat": fmt["input"],
                "OutputFormat": fmt["output"],
                "SerdeInfo": {"SerializationLibrary": fmt["serde"], "Parameters": fmt["params"]},
            },
        },
    )
    return {
        "type": "glue",
        "database": database,
        "table": dest["table"],
        "location": location,
        "format": dest["format"],
        "part_files": parts,
        "region": REGION,
        "athena_query": f'SELECT * FROM "{database}"."{dest["table"]}" LIMIT 10;',
    }


# =================================================
# BROWSING
# =================================================


# What already exists at a location, for the dropdowns
def browse(dest: dict) -> dict:
    """
    List existing schemas/tables/datasets at a partial destination.

    Arguments:
        dest (dict): validated destination without a table

    Returns:
        dict: {"schemas": {schema: [tables]}} for SQL, {"datasets": [prefixes]} for s3,
              {"tables": [names]} for dynamodb and glue
    """
    source = dest["source"]

    # Postgres / Redshift: every schema and its tables
    if source in ("postgres", "redshift"):
        rows = pg_run(dest["db"], [SCHEMAS_SQL]) if source == "postgres" else rs_exec(dest["db"], [SCHEMAS_SQL])[1]
        schemas = {}
        for schema, table in rows:
            schemas.setdefault(schema, [])
            if table:
                schemas[schema].append(table)
        return {"schemas": schemas}

    # DynamoDB tables made by this app
    if source == "dynamodb":
        return {"tables": [table["TableName"] for table in app_tables()]}

    # S3: every prefix under mock-data/ that holds part files
    if source == "s3":
        datasets = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=APP_BUCKET, Prefix=DATA_PREFIX):
            for obj in page.get("Contents", []):
                match = PART_RE.match(obj["Key"][len(DATA_PREFIX) :])
                if match and match.group(1) not in datasets:
                    datasets.append(match.group(1))
        return {"datasets": datasets}

    # Glue: tables in the mock_data database (none yet if it doesn't exist)
    tables = []
    try:
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=GLUE_DATABASE):
            for table in page["TableList"]:
                tables.append(table["Name"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    return {"tables": tables}


# =================================================
# DELETING
# =================================================


# Delete a table or dataset
def delete_table(dest: dict) -> None:
    """
    Delete a table. For s3 this deletes the dataset's part files; for glue the catalog table and its files.

    Arguments:
        dest (dict): validated destination with a table
    """
    source = dest["source"]
    qualified = f'"{dest.get("schema")}"."{dest.get("table")}"'
    if source == "postgres":
        pg_run(dest["db"], [f"DROP TABLE IF EXISTS {qualified}"])
    elif source == "redshift":
        rs_exec(dest["db"], [f"DROP TABLE IF EXISTS {qualified}"])
    elif source == "dynamodb":
        require_app_table(dest["table"])
        dynamo.delete_table(TableName=dest["table"])
    elif source == "s3":
        delete_dataset(APP_BUCKET, dataset_folder(dest))
    elif source == "glue":
        location = glue.get_table(DatabaseName=GLUE_DATABASE, Name=dest["table"])["Table"]["StorageDescriptor"]["Location"]
        # Only delete files under the app bucket's mock-tables/ prefix
        if location.startswith(GLUE_ROOT):
            delete_dataset(APP_BUCKET, location[len(f"s3://{APP_BUCKET}/") :])
        glue.delete_table(DatabaseName=GLUE_DATABASE, Name=dest["table"])
