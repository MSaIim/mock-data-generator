# Mock Data Generator

A small web app that generates fake data with [Faker](https://faker.readthedocs.io/) and writes it to PostgreSQL, Redshift, DynamoDB, S3 or Glue. It runs as a single Lambda behind an internal load balancer.

- Pick a destination, define columns (types, % nulls, % junk), and generate, in batches if you like.
- S3 and Glue datasets can be CSV, JSON, Parquet or Avro, written as part files.
- A datasets view lists everything that exists, with metadata and a preview.
- Admins create Postgres and Redshift databases and manage users.

## Run locally

Local mode uses SQLite and local folders in place of AWS, and never contacts AWS.

```bash
pip install -r requirements.txt
python dev/local.py    # http://localhost:8000
```

The first visit asks you to create the admin account. Data goes in `data/`.

## Deploy to AWS

1. `python dev/build.py` builds `lambda-mock-gen.zip`, with Linux wheels for the Lambda runtime. CI builds it too (the `lambda-mock-gen` artifact).
2. Upload `lambda-mock-gen.zip` **and** `template.yaml` to an S3 bucket in the region you'll deploy to.
3. In CloudFormation, choose **Create stack → Choose an existing template → Amazon S3 URL**, and paste the URL of `template.yaml` (for example `https://<bucket>.s3.<region>.amazonaws.com/template.yaml`). Using the S3 URL avoids the console's "Upload a template file" option, which needs permission to create its own S3 bucket. You'll need:
   - the VPC
   - 3 private subnets in 3 AZs, with a NAT gateway
   - the network range allowed to reach the app
   - the code bucket
   - optionally, an ACM certificate for HTTPS
4. On the last page, acknowledge that the stack creates IAM roles.
5. Open the `AppUrl` output from inside your network and create the admin account.

## Layout

| Path | What |
|---|---|
| `src/app.py` | Lambda handler: routing, validation |
| `src/data/` | Fake data generation and file formats |
| `src/store/` | Metadata store (DynamoDB / SQLite), users and sessions |
| `src/aws/` | AWS backend: databases, writers, catalog |
| `src/local/` | SQLite stand-in used by `dev/local.py` |
| `src/ui/` | Single-page UI (HTML, CSS, JS) |
| `template.yaml` | CloudFormation stack |
| `dev/build.py` | Builds the Lambda zip from `requirements.txt` |
| `dev/local.py` | Runs the app locally |
| `.github/workflows/build.yml` | CI: builds the zip on every push and PR |
