# Manual deployment (without CloudFormation)

Use this if CloudFormation can't create the stack (for example, because of permission errors). These steps create the same resources as `template.yaml`, by hand in the AWS console.

> Doing this by hand uses **your** permissions, just as CloudFormation does. If CloudFormation was refused a step (often creating IAM roles), the console will refuse it too. Ask an admin to create the two IAM roles in step 5 with the JSON below, then do the rest yourself.

Build everything in the **same region** as the bucket that holds `lambda-mock-gen.zip`. The names below use a `mockdata-` prefix; any names work, with one exception:

> ⚠️ **Don't start the table or staging bucket names with `mockgen-`.** The app treats anything named `mockgen-…` as a user dataset, so it would show up in the Datasets modal and could be deleted from there.

## 1. Security groups

EC2 → Security Groups → Create. Put all three in your VPC.

| Name | Inbound port | Source (Type: Custom) |
|---|---|---|
| `mockdata-alb` | TCP 80 (443 for HTTPS) | Your allowed network range (AllowedCidr) |
| `mockdata-lambda` | none | No inbound rules |
| `mockdata-db` | TCP 5432 | The `mockdata-lambda` security group (type `sg-` or the name in the source box) |
| `mockdata-db` | TCP 5432 | AllowedCidr |
| `mockdata-db` | TCP 5439 | AllowedCidr |

- The Lambda needs no inbound rules: the load balancer invokes it through AWS, not over the network.
- **AllowedCidr** is the range your users connect *from*. That's your VPC range if they come in through AWS Client VPN or a host inside the VPC. If they come from an office or a VPN that keeps their own addresses, use that range instead.

**Outbound:** leave the default **All traffic → 0.0.0.0/0** on all three groups.

- `mockdata-lambda` needs it to reach AWS services through the NAT gateway (port 443) and Postgres (5432). Without it, every request times out.
- If outbound must be tightened, the minimum for `mockdata-lambda` is TCP 443 to `0.0.0.0/0` plus TCP 5432 to the `mockdata-db` group.
- `mockdata-alb` and `mockdata-db` don't depend on outbound rules. Security groups allow replies to allowed inbound traffic automatically.

## 2. DB subnet group

RDS → Subnet groups → Create. Name it `mockdata-db-subnets`, then pick your VPC and the 3 private subnets.

## 3. DynamoDB table

DynamoDB → Create table:

- Name: `mockdata-meta`
- Partition key: `pk` (String)
- Table settings → Customize → Capacity mode: **On-demand**

## 4. Staging S3 bucket

S3 → Create bucket. Name it something like `mockdata-staging-<account-id>`. Keep **Block all public access** on.

## 5. IAM roles

IAM → Roles → Create role.

### a) `mockdata-redshift-copy`

Trusted entity: **Custom trust policy**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": [
          "redshift.amazonaws.com",
          "redshift-serverless.amazonaws.com"
        ]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Inline policy (replace `<STAGING_BUCKET>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<STAGING_BUCKET>"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<STAGING_BUCKET>/*"
    }
  ]
}
```

### b) `mockdata-lambda`

Trusted entity: AWS service → **Lambda**. Attach the managed policy **AWSLambdaVPCAccessExecutionRole**, then add this inline policy. Replace `<REGION>`, `<ACCOUNT>`, `<META_TABLE>`, `<STAGING_BUCKET>` and `<COPY_ROLE_ARN>` (the ARN of `mockdata-redshift-copy`).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT>:table/<META_TABLE>"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable",
        "dynamodb:DeleteTable",
        "dynamodb:DescribeTable",
        "dynamodb:BatchWriteItem",
        "dynamodb:PutItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT>:table/mockgen-*"
    },
    {
      "Effect": "Allow",
      "Action": "dynamodb:ListTables",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::mockgen-<ACCOUNT>-*",
        "arn:aws:s3:::mockgen-<ACCOUNT>-*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListAllMyBuckets",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::<STAGING_BUCKET>",
        "arn:aws:s3:::<STAGING_BUCKET>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "glue:GetDatabases",
      "Resource": [
        "arn:aws:glue:<REGION>:<ACCOUNT>:catalog",
        "arn:aws:glue:<REGION>:<ACCOUNT>:database/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "glue:CreateDatabase",
        "glue:GetDatabase",
        "glue:GetTable",
        "glue:GetTables",
        "glue:CreateTable",
        "glue:DeleteTable"
      ],
      "Resource": [
        "arn:aws:glue:<REGION>:<ACCOUNT>:catalog",
        "arn:aws:glue:<REGION>:<ACCOUNT>:database/mockgen_*",
        "arn:aws:glue:<REGION>:<ACCOUNT>:table/mockgen_*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBCluster",
        "rds:CreateDBInstance",
        "rds:DeleteDBCluster",
        "rds:DeleteDBInstance",
        "rds:DescribeDBClusters",
        "rds:DescribeDBInstances",
        "rds:AddTagsToResource"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "redshift-serverless:CreateNamespace",
        "redshift-serverless:CreateWorkgroup",
        "redshift-serverless:DeleteNamespace",
        "redshift-serverless:DeleteWorkgroup",
        "redshift-serverless:GetNamespace",
        "redshift-serverless:GetWorkgroup",
        "redshift-serverless:TagResource",
        "redshift-serverless:GetCredentials"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "redshift-data:ExecuteStatement",
        "redshift-data:BatchExecuteStatement",
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "<COPY_ROLE_ARN>"
    },
    {
      "Effect": "Allow",
      "Action": "ec2:Describe*",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "*",
      "Condition": {
        "StringLike": {
          "iam:AWSServiceName": [
            "rds.amazonaws.com",
            "redshift.amazonaws.com",
            "redshift-serverless.amazonaws.com"
          ]
        }
      }
    }
  ]
}
```

## 6. Lambda function

Lambda → Create function → Author from scratch:

1. Name: `mockdata-app`. Runtime **Python 3.13**, architecture **x86_64**. Permissions: **Use an existing role** → `mockdata-lambda`.
2. **Code → Upload from → Amazon S3 location**: paste the S3 URL of `lambda-mock-gen.zip`.
3. **Runtime settings → Edit**: Handler `app.handler`.
4. **Configuration → General configuration**: Memory **2048 MB**, Timeout **15 min 0 sec**.
5. **Configuration → VPC**: your VPC, the 3 private subnets, security group `mockdata-lambda`.
6. **Configuration → Environment variables**:

| Key | Value |
|---|---|
| `META_TABLE` | `mockdata-meta` |
| `STAGING_BUCKET` | your staging bucket name |
| `BUCKET_PREFIX` | `mockgen-<account-id>-` (keep the trailing dash) |
| `DB_SUBNET_GROUP` | `mockdata-db-subnets` |
| `DB_SECURITY_GROUP` | the **ID** of `mockdata-db` (`sg-…`) |
| `SUBNET_IDS` | `subnet-a,subnet-b,subnet-c` (comma-separated, no spaces) |
| `REDSHIFT_ROLE_ARN` | ARN of `mockdata-redshift-copy` |
| `SECURE_COOKIE` | `false` (`true` if you use HTTPS) |
| `MOCKGEN_LOCAL` | `false` |

## 7. Target group

EC2 → Target groups → Create:

- Target type: **Lambda function**
- Name: `mockdata-tg`
- Function: `mockdata-app`. The console grants the load balancer permission to invoke it automatically.

## 8. Load balancer

EC2 → Load balancers → Create → **Application Load Balancer**:

1. Name: `mockdata-alb`. Scheme **Internal**, IP type **IPv4**.
2. Your VPC and the same 3 subnets. Security group: `mockdata-alb` (remove the default group).
3. Listener: **HTTP 80**, forwarding to `mockdata-tg`. For HTTPS, use **443** with your ACM certificate instead.
4. After creation: **Attributes → Edit → Connection idle timeout = 900 seconds**, so long generations aren't cut off.

## 9. Open it

Browse to `http://<load balancer DNS name>` from inside your network. The first visit asks you to create the admin account.
