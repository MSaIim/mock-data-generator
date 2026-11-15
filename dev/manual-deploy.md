# Manual deployment (without CloudFormation)

Use this if CloudFormation can't create the stack (for example, because of permission errors). These steps create the same resources as `template.yaml`, by hand in the AWS console.

> Doing this by hand uses **your** permissions, just as CloudFormation does. If CloudFormation was refused a step (often creating IAM roles), the console will refuse it too. Ask an admin to create the IAM role in step 4 with the JSON below, then do the rest yourself.

Build everything in **us-east-1**, the same region as the bucket that holds `lambda-mock-gen.zip`. The names below are the ones used for this deployment (`dq-poc-mock-data-*`).

## 1. Security groups

EC2 → Security Groups → Create. Put all three in your VPC.

| Name | Inbound port | Source (Type: Custom) |
|---|---|---|
| `dq-poc-mock-data-alb` | TCP 80 (443 for HTTPS) | Your allowed network range (AllowedCidr) |
| `dq-poc-mock-data-lambda` | none | No inbound rules |
| `dq-poc-mock-data-db` | TCP 5432 | The `dq-poc-mock-data-lambda` security group (type `sg-` or the name in the source box) |
| `dq-poc-mock-data-db` | TCP 5432 | AllowedCidr |
| `dq-poc-mock-data-db` | TCP 5439 | AllowedCidr |

- The Lambda needs no inbound rules: the load balancer invokes it through AWS, not over the network.
- **AllowedCidr** is the range your users connect *from*. That's your VPC range if they come in through AWS Client VPN or a host inside the VPC. If they come from an office or a VPN that keeps their own addresses, use that range instead. For example, if `ipconfig` on the VPN shows `100.127.x.x`, that address must be allowed even if the VPC is `100.124.x.x`. Ask your network admin for the VPN's client range.

**Outbound:** leave the default **All traffic → 0.0.0.0/0** on all three groups.

- `dq-poc-mock-data-lambda` needs it to reach AWS services through the NAT gateway (port 443) and Postgres (5432). Without it, every request times out.
- If outbound must be tightened, the minimum for `dq-poc-mock-data-lambda` is TCP 443 to `0.0.0.0/0` plus TCP 5432 to the `dq-poc-mock-data-db` group.
- `dq-poc-mock-data-alb` and `dq-poc-mock-data-db` don't depend on outbound rules. Security groups allow replies to allowed inbound traffic automatically.

## 2. DB subnet group

RDS → Subnet groups → Create. Name it `dq-poc-mock-data-subnets`, then pick your VPC and the 3 private subnets.

## 3. App S3 bucket

The app uses the existing bucket `vgi-cdao-data-quality-poc`, so there's nothing to create. It never creates or deletes buckets. It only writes under four prefixes, and the IAM policy below limits it to them:

- `mock-data/`: S3 datasets. The prefix you enter is the dataset (`mock-data/<prefix>/part-00000.<format>`).
- `tables/`: the files behind Glue tables (`tables/<table>/`). Every Glue table goes in the Glue database `mock_data`, which the app creates on first use if it doesn't exist.
- `dependencies/DB/<name>.json`: one file per Postgres/Redshift database, holding its status, endpoint and credentials. The bucket's default encryption (SSE-S3) encrypts them at rest.
- `staging/`: Redshift `COPY` files, deleted after each load.

Postgres, Redshift and DynamoDB names are used exactly as entered. The app tags every DynamoDB table it creates (`created-by = mock-data-generator`), and only lists, replaces or deletes tables with that tag. Other tables in the account are never touched.

## 4. IAM role

One role, `dq-poc-mock-data-iam`, used by both the Lambda (to run the app) and Redshift (to read staged files during `COPY`).

IAM → Roles → Create role:

1. Trusted entity: **Custom trust policy**, so all three services can use the role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": [
          "lambda.amazonaws.com",
          "redshift.amazonaws.com",
          "redshift-serverless.amazonaws.com"
        ]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

2. Skip the "Add permissions" page (no AWS managed policies). Name it `dq-poc-mock-data-iam` and create it. Copy its ARN.
3. Open the role → **Add permissions → Create inline policy → JSON**, and paste the policy below. It's the role's only policy and lists every permission explicitly, including CloudWatch Logs and the network interface permissions the Lambda needs to run in your VPC. Replace `<ACCOUNT>` with your 12-digit account ID; everything else is already filled in for this deployment. The `iam:PassRole` statement points at this role's own ARN: when someone creates a Redshift database, the app hands this role to Redshift, so it must be allowed to pass itself.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LambdaLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:<ACCOUNT>:*"
    },
    {
      "Sid": "LambdaVpcNetworking",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable",
        "dynamodb:DeleteTable",
        "dynamodb:DescribeTable",
        "dynamodb:TagResource",
        "dynamodb:ListTagsOfResource",
        "dynamodb:BatchWriteItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:<ACCOUNT>:table/*"
    },
    {
      "Effect": "Allow",
      "Action": "dynamodb:ListTables",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::vgi-cdao-data-quality-poc"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::vgi-cdao-data-quality-poc/mock-data/*",
        "arn:aws:s3:::vgi-cdao-data-quality-poc/tables/*",
        "arn:aws:s3:::vgi-cdao-data-quality-poc/dependencies/*",
        "arn:aws:s3:::vgi-cdao-data-quality-poc/staging/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:CreateDatabase",
        "glue:GetTable",
        "glue:GetTables",
        "glue:CreateTable",
        "glue:DeleteTable"
      ],
      "Resource": [
        "arn:aws:glue:us-east-1:<ACCOUNT>:catalog",
        "arn:aws:glue:us-east-1:<ACCOUNT>:database/mock_data",
        "arn:aws:glue:us-east-1:<ACCOUNT>:table/mock_data/*"
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
        "rds:DescribeDBInstances"
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
      "Resource": "arn:aws:iam::<ACCOUNT>:role/dq-poc-mock-data-iam"
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

## 5. Lambda function

Lambda → Create function → Author from scratch:

1. Name: `dq-poc-mock-data-app`. Runtime **Python 3.13**, architecture **x86_64**. Permissions: **Use an existing role** → `dq-poc-mock-data-iam`.
2. Click **Create function**. Then on the **Code** tab: **Upload from → Amazon S3 location**, and paste the zip's Object URL, e.g. `https://<code-bucket>.s3.us-east-1.amazonaws.com/dependencies/lambda-mock-gen.zip`. Replacing the file in S3 later doesn't update the function; upload it here again.
3. **Runtime settings → Edit**: Handler `app.handler`.
4. **Configuration → General configuration**: Memory **2048 MB**, Timeout **15 min 0 sec**.

Each settings page has its own **Save**; changes are lost if you leave the page without it.
5. **Configuration → VPC**: your VPC, the 3 private subnets, security group `dq-poc-mock-data-lambda`.
6. **Configuration → Environment variables**:

| Key | Value |
|---|---|
| `APP_BUCKET` | `vgi-cdao-data-quality-poc` |
| `DB_SUBNET_GROUP` | `dq-poc-mock-data-subnets` |
| `DB_SECURITY_GROUP` | the **ID** of `dq-poc-mock-data-db` (`sg-…`) |
| `SUBNET_IDS` | `subnet-a,subnet-b,subnet-c` (comma-separated, no spaces) |
| `REDSHIFT_ROLE_ARN` | `arn:aws:iam::<ACCOUNT>:role/dq-poc-mock-data-iam` |
| `MOCKGEN_LOCAL` | `false` |

## 6. Target group

EC2 → Target groups → Create:

- Target type: **Lambda function**
- Name: `dq-poc-mock-data-tg`
- Function: `dq-poc-mock-data-app`. The console grants the load balancer permission to invoke it automatically.

## 7. Load balancer

EC2 → Load balancers → Create → **Application Load Balancer**:

1. Name: `dq-poc-mock-data-alb`. Scheme **Internal**, IP type **IPv4**.
2. Your VPC and the same 3 subnets. Security group: `dq-poc-mock-data-alb` (remove the default group).
3. Listener: **HTTP 80**, forwarding to `dq-poc-mock-data-tg`. For HTTPS, use **443** with your ACM certificate instead.
4. After creation: **Attributes → Edit → Connection idle timeout = 900 seconds**, so long generations aren't cut off.

## 8. Open it

Browse to `http://<load balancer DNS name>` (`http`, not `https`, unless you set up a certificate) from inside your network. There are no accounts; the app opens straight into the generator.

If it fails, check the function's logs (Lambda → Monitor → View CloudWatch logs):

- `No module named 'lambda_function'`: the handler isn't `app.handler`.
- `No module named 'pyarrow.lib'`: the runtime isn't Python 3.13 or the architecture isn't x86_64.
- `KeyError: 'SOME_NAME'`: that environment variable is missing.
- A timeout with no logs at all: the request isn't reaching the load balancer. Check the `dq-poc-mock-data-alb` inbound rule allows your address.
