import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

from store import meta

# =================================================
# CONFIG AND CLIENTS
# =================================================

DB_NAME = "mockdata"
DB_USER = "mockadmin"
ENGINES = ("postgres", "redshift")

SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
DB_SECURITY_GROUP = os.environ["DB_SECURITY_GROUP"]
DB_SUBNET_GROUP = os.environ["DB_SUBNET_GROUP"]
REDSHIFT_ROLE_ARN = os.environ["REDSHIFT_ROLE_ARN"]

rds = boto3.client("rds")
redshift = boto3.client("redshift-serverless")

# =================================================
# HELPERS
# =================================================


# Call an AWS API and return None instead of raising when the resource doesn't exist
def missing_ok(call, not_found_code: str, **kwargs):
    """
    Run an AWS call, treating a "not found" error as None.

    Arguments:
        call (callable): boto3 client method
        not_found_code (str): error code that means the resource is gone
        **kwargs: arguments for the call

    Returns:
        dict | None: the response, or None when not found
    """
    try:
        return call(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != not_found_code:
            raise
        return None


# Load a database record from the metadata table
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
    return {
        "name": record["name"],
        "engine": record["engine"],
        "status": record["status"],
        "error": record.get("error", ""),
        "created_by": record.get("created_by", ""),
    }


# =================================================
# PUBLIC API
# =================================================


# List every registered database
def list_databases() -> list:
    """
    List all databases in the metadata table.

    Returns:
        list: public database records, sorted by name
    """
    result = [public(item) for item in meta.scan("DB#")]
    result.sort(key=lambda r: r["name"])
    return result


# Start creating a Postgres or Redshift database
def create_database(name: str, engine: str, created_by: str) -> dict:
    """
    Register the database and start creating it. Creation takes several minutes;
    refresh_database reports when it's ready.

    Arguments:
        name (str): validated database name
        engine (str): postgres or redshift
        created_by (str): admin username

    Returns:
        dict: public record
    """
    ident = "mockgen-" + name
    # Upper, lower and digit are guaranteed so the password passes Redshift's rules
    password = secrets.token_urlsafe(18) + "Aa1"

    # Register first (with the credentials) so a failed create can still be cleaned up from the admin page
    record = {
        "pk": "DB#" + name,
        "name": name,
        "engine": engine,
        "ident": ident,
        "database": DB_NAME,
        "username": DB_USER,
        "password": password,
        "status": "creating",
        "created_by": created_by,
        "created_at": int(time.time()),
    }
    if not meta.put_new(record):
        raise ValueError(f"A database named {name} already exists")

    try:
        # Aurora PostgreSQL Serverless v2, pausing at 0 capacity when idle
        if engine == "postgres":
            cluster = rds.create_db_cluster(
                DBClusterIdentifier=ident,
                Engine="aurora-postgresql",
                DatabaseName=DB_NAME,
                MasterUsername=DB_USER,
                MasterUserPassword=password,
                DBSubnetGroupName=DB_SUBNET_GROUP,
                VpcSecurityGroupIds=[DB_SECURITY_GROUP],
                StorageEncrypted=True,
                ServerlessV2ScalingConfiguration={"MinCapacity": 0, "MaxCapacity": 4},
            )["DBCluster"]
            rds.create_db_instance(
                DBInstanceIdentifier=ident + "-1",
                DBClusterIdentifier=ident,
                Engine="aurora-postgresql",
                DBInstanceClass="db.serverless",
            )
            arn = cluster["DBClusterArn"]

        # Redshift Serverless: namespace (storage) plus workgroup (compute)
        else:
            redshift.create_namespace(
                namespaceName=ident,
                dbName=DB_NAME,
                adminUsername=DB_USER,
                adminUserPassword=password,
                iamRoles=[REDSHIFT_ROLE_ARN],
                defaultIamRoleArn=REDSHIFT_ROLE_ARN,
            )
            # The workgroup can only be created once the namespace is available (usually a few seconds)
            for _ in range(60):
                if redshift.get_namespace(namespaceName=ident)["namespace"]["status"] == "AVAILABLE":
                    break
                time.sleep(2)
            workgroup = redshift.create_workgroup(
                workgroupName=ident,
                namespaceName=ident,
                baseCapacity=8,
                subnetIds=SUBNET_IDS,
                securityGroupIds=[DB_SECURITY_GROUP],
                publiclyAccessible=False,
            )["workgroup"]
            arn = workgroup["workgroupArn"]

        record["arn"] = arn
        meta.put(record)
    except ClientError as e:
        # Keep the record so the admin sees the failure and can delete it
        record["status"] = "failed"
        record["error"] = e.response["Error"]["Message"]
        meta.put(record)
        raise

    return public(record)


# Check whether a database is up, finish deletions, and return its details
def refresh_database(name: str) -> dict:
    """
    Check AWS for the database's current state and update the record.
    Returns connection info once the database is available.

    Arguments:
        name (str): database name

    Returns:
        dict: public record, plus "connection" when available
    """
    record = get_record(name)
    ident = record["ident"]
    host = None
    port = None

    # Postgres: the cluster and its instance must both be available
    if record["engine"] == "postgres":
        clusters = missing_ok(rds.describe_db_clusters, "DBClusterNotFoundFault", DBClusterIdentifier=ident)
        instances = missing_ok(rds.describe_db_instances, "DBInstanceNotFound", DBInstanceIdentifier=ident + "-1")
        cluster = clusters["DBClusters"][0] if clusters else None
        instance = instances["DBInstances"][0] if instances else None
        gone = cluster is None
        up = cluster is not None and instance is not None and cluster["Status"] == "available" and instance["DBInstanceStatus"] == "available"
        if up:
            host = cluster["Endpoint"]
            port = cluster["Port"]

    # Redshift: the namespace and workgroup must both be available
    else:
        wg_resp = missing_ok(redshift.get_workgroup, "ResourceNotFoundException", workgroupName=ident)
        ns_resp = missing_ok(redshift.get_namespace, "ResourceNotFoundException", namespaceName=ident)
        workgroup = wg_resp["workgroup"] if wg_resp else None
        namespace = ns_resp["namespace"] if ns_resp else None
        # The namespace can only be deleted after its workgroup is gone
        if record["status"] == "deleting" and workgroup is None and namespace is not None and namespace["status"] != "DELETING":
            redshift.delete_namespace(namespaceName=ident)
        gone = workgroup is None and namespace is None
        up = workgroup is not None and namespace is not None and workgroup["status"] == "AVAILABLE" and namespace["status"] == "AVAILABLE"
        if up:
            host = workgroup["endpoint"]["address"]
            port = workgroup["endpoint"]["port"]

    # Finish a deletion once everything is gone
    if record["status"] == "deleting":
        if gone:
            meta.delete(record["pk"])
            return {**public(record), "status": "deleted"}
        return public(record)

    # Mark as available and return connection info
    if up:
        record["status"] = "available"
        record["host"] = host
        record["port"] = port
        record.pop("error", None)
        meta.put(record)
        return {**public(record), "connection": connection_info(record)}
    return public(record)


# Start deleting a database
def delete_database(name: str) -> dict:
    """
    Start deleting the database's AWS resources. refresh_database finishes the cleanup once they're gone.

    Arguments:
        name (str): database name

    Returns:
        dict: public record
    """
    record = get_record(name)
    ident = record["ident"]

    # Postgres: delete the instance, then the cluster (allowed while the instance is deleting)
    if record["engine"] == "postgres":
        missing_ok(rds.delete_db_instance, "DBInstanceNotFound", DBInstanceIdentifier=ident + "-1")
        missing_ok(rds.delete_db_cluster, "DBClusterNotFoundFault", DBClusterIdentifier=ident, SkipFinalSnapshot=True)
    # Redshift: delete the workgroup now, the namespace on a later refresh
    else:
        missing_ok(redshift.delete_workgroup, "ResourceNotFoundException", workgroupName=ident)

    record["status"] = "deleting"
    meta.put(record)
    return public(record)


# Load a database that is ready for use
def get_database(name: str, engine: str) -> dict:
    """
    Load a database that is ready for use, including its credentials.

    Arguments:
        name (str): database name
        engine (str): expected engine (postgres or redshift)

    Returns:
        dict: full record
    """
    record = get_record(name)
    if record["engine"] != engine:
        raise ValueError(f"{name} is not a {engine} database")
    if record["status"] != "available":
        raise ValueError(f"{name} is not ready yet")
    return record


# Connection details a user needs to connect with a SQL client
def connection_info(db: dict) -> dict:
    """
    Build the connection details for a database.

    Arguments:
        db (dict): record from get_database

    Returns:
        dict: host, port, database, username, password and a ready-made connection string
    """
    port = int(db["port"])
    info = {
        "type": db["engine"],
        "host": db["host"],
        "port": port,
        "database": db["database"],
        "username": db["username"],
        "password": db["password"],
    }
    if db["engine"] == "postgres":
        info["psql"] = f'psql "host={db["host"]} port={port} dbname={db["database"]} user={db["username"]} sslmode=require"'
    else:
        info["jdbc"] = f"jdbc:redshift://{db['host']}:{port}/{db['database']}"
    return info
