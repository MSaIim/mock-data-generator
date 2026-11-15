import pg8000.native
from botocore.exceptions import ClientError

from aws import writers
from data import formats

# =================================================
# DATASET CATALOG AND PREVIEWS (AWS)
# =================================================

SAMPLE_ROWS = 10

# Every user table's columns (Postgres and Redshift), excluding system schemas
COLUMNS_SQL = (
    "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
    "WHERE substring(table_schema, 1, 3) <> 'pg_' AND table_schema <> 'information_schema' "
    "ORDER BY table_schema, table_name, ordinal_position"
)


# List every dataset across all sources
def catalog(dbs: list) -> dict:
    """
    List every dataset this tool can see, with metadata. A failing source is reported in "errors"
    instead of failing the whole list.

    Arguments:
        dbs (list): available Postgres/Redshift database records (with credentials)

    Returns:
        dict: {"datasets": [...], "errors": [...]}. Each dataset has source, name, location, dest
              (fields for the sample action) and whichever of columns, format, rows, files, size, updated apply
    """
    datasets = []
    errors = []

    # Postgres / Redshift: tables and columns per database
    for db in dbs:
        try:
            if db["engine"] == "postgres":
                rows = writers.pg_run(db, [COLUMNS_SQL])
            else:
                rows = writers.rs_exec(db, [COLUMNS_SQL])[1]
            tables = {}
            for schema, table, column, data_type in rows:
                tables.setdefault((schema, table), []).append({"name": column, "type": data_type})
            for (schema, table), columns in tables.items():
                datasets.append(
                    {
                        "source": db["engine"],
                        "name": table,
                        "location": f"{db['name']}/{schema}.{table}",
                        "dest": {
                            "database": db["name"],
                            "schema": schema,
                            "table": table,
                        },
                        "columns": columns,
                    }
                )
        except (ValueError, ClientError, pg8000.native.InterfaceError, OSError) as e:
            errors.append(f"{db['engine']} {db['name']}: {e}")

    # DynamoDB: approximate item count and size from DescribeTable
    try:
        for page in writers.dynamo.get_paginator("list_tables").paginate():
            for name in page["TableNames"]:
                if not name.startswith(writers.DYNAMO_PREFIX):
                    continue
                table = writers.dynamo.describe_table(TableName=name)["Table"]
                short = name[len(writers.DYNAMO_PREFIX) :]
                datasets.append(
                    {
                        "source": "dynamodb",
                        "name": short,
                        "location": name,
                        "dest": {"table": short},
                        "rows": table.get("ItemCount"),
                        "rows_estimate": True,
                        "size": table.get("TableSizeBytes"),
                        "updated": table["CreationDateTime"].isoformat(),
                    }
                )
    except ClientError as e:
        errors.append(f"dynamodb: {e.response['Error']['Message']}")

    # S3: group part files into datasets
    try:
        for short in writers.list_buckets():
            found = {}
            for page in writers.s3.get_paginator("list_objects_v2").paginate(Bucket=writers.BUCKET_PREFIX + short):
                for obj in page.get("Contents", []):
                    match = writers.PART_RE.match(obj["Key"])
                    if not match:
                        continue
                    entry = found.setdefault(
                        (match.group(1) or "", match.group(2)),
                        {
                            "files": 0,
                            "size": 0,
                            "updated": obj["LastModified"],
                            "format": obj["Key"].rsplit(".", 1)[1],
                        },
                    )
                    entry["files"] += 1
                    entry["size"] += obj["Size"]
                    entry["updated"] = max(entry["updated"], obj["LastModified"])
            for (prefix, table), entry in found.items():
                datasets.append(
                    {
                        "source": "s3",
                        "name": table,
                        "location": f"s3://{short}/{prefix + '/' if prefix else ''}{table}/",
                        "dest": {"bucket": short, "prefix": prefix, "table": table},
                        "format": entry["format"],
                        "files": entry["files"],
                        "size": entry["size"],
                        "updated": entry["updated"].isoformat(),
                    }
                )
    except ClientError as e:
        errors.append(f"s3: {e.response['Error']['Message']}")

    # Glue: catalog tables with their columns and location
    try:
        for short in writers.list_glue_databases():
            for page in writers.glue.get_paginator("get_tables").paginate(DatabaseName=writers.GLUE_PREFIX + short):
                for table in page["TableList"]:
                    storage = table["StorageDescriptor"]
                    updated = table.get("UpdateTime") or table.get("CreateTime")
                    datasets.append(
                        {
                            "source": "glue",
                            "name": table["Name"],
                            "location": f"{short}.{table['Name']} → {storage['Location']}",
                            "dest": {"database": short, "table": table["Name"]},
                            "format": table.get("Parameters", {}).get("classification"),
                            "columns": [{"name": c["Name"], "type": c["Type"]} for c in storage["Columns"]],
                            "updated": updated.isoformat() if updated else None,
                        }
                    )
    except ClientError as e:
        errors.append(f"glue: {e.response['Error']['Message']}")

    return {"datasets": datasets, "errors": errors}


# First rows of a dataset
def sample(dest: dict) -> dict:
    """
    Read the first rows of a dataset. SQL sources also return an exact row count.

    Arguments:
        dest (dict): validated destination with a table

    Returns:
        dict: {"columns": [names], "rows": [dicts], "row_count": int or None, "types": {name: type} for Parquet/Avro files}
    """
    source = dest["source"]
    qualified = f'"{dest.get("schema")}"."{dest["table"]}"'

    # Postgres
    if source == "postgres":
        con = writers.pg_connect(dest["db"])
        try:
            rows = con.run(f"SELECT * FROM {qualified} LIMIT {SAMPLE_ROWS}")
            names = [column["name"] for column in con.columns]
            count = con.run(f"SELECT COUNT(*) FROM {qualified}")[0][0]
        except pg8000.native.DatabaseError as e:
            raise ValueError(f"Postgres: {e}")
        finally:
            con.close()
        return {"columns": names, "rows": [dict(zip(names, row)) for row in rows], "row_count": count}

    # Redshift
    if source == "redshift":
        names, rows = writers.rs_exec(dest["db"], [f"SELECT * FROM {qualified} LIMIT {SAMPLE_ROWS}"])
        count = writers.rs_exec(dest["db"], [f"SELECT COUNT(*) FROM {qualified}"])[1][0][0]
        return {"columns": names, "rows": [dict(zip(names, row)) for row in rows], "row_count": int(count)}

    # DynamoDB: a short scan (items can have different attributes)
    if source == "dynamodb":
        items = writers.dynamo_resource.Table(writers.DYNAMO_PREFIX + dest["table"]).scan(Limit=SAMPLE_ROWS)["Items"]
        names = ["pk"]
        for item in items:
            for key in item:
                if key not in names:
                    names.append(key)
        return {"columns": names, "rows": items, "row_count": None}

    # S3 / Glue: decode the first part file
    if source == "s3":
        bucket = writers.BUCKET_PREFIX + dest["bucket"]
        folder = writers.dataset_folder(dest)
        hive_csv = False
    else:
        table = writers.glue.get_table(DatabaseName=writers.GLUE_PREFIX + dest["database"], Name=dest["table"])["Table"]
        location = table["StorageDescriptor"]["Location"]
        if not location.startswith("s3://" + writers.BUCKET_PREFIX):
            raise ValueError("This table's files aren't in a bucket this tool manages")
        bucket, _, folder = location[len("s3://") :].partition("/")
        hive_csv = True
    listing = writers.s3.list_objects_v2(Bucket=bucket, Prefix=folder, Delimiter="/")
    parts = sorted(obj["Key"] for obj in listing.get("Contents", []) if writers.PART_RE.match(obj["Key"]))
    if not parts:
        return {"columns": [], "rows": [], "row_count": 0}
    data = writers.s3.get_object(Bucket=bucket, Key=parts[0])["Body"].read()
    names, rows, types = formats.decode(parts[0].rsplit(".", 1)[1], data, hive_csv, SAMPLE_ROWS)
    return {"columns": names, "rows": rows, "types": types, "row_count": None}
