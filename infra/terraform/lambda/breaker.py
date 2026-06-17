"""Kill switch: an invocation-spike alarm trips this via SNS; it sets the proxy
Lambda's reserved concurrency to 0, cutting off all QA (and LLM spend) until a human
re-arms it with `terraform apply` (which resets concurrency to proxy_max_concurrency).

The built-in Lambda boto3 is fine here (only put_function_concurrency is used).

env: PROXY_FUNCTION_NAME
"""
import os

import boto3

FUNCTION = os.environ["PROXY_FUNCTION_NAME"]


def lambda_handler(event, context):
    boto3.client("lambda").put_function_concurrency(
        FunctionName=FUNCTION,
        ReservedConcurrentExecutions=0,
    )
    return {"tripped": FUNCTION}
