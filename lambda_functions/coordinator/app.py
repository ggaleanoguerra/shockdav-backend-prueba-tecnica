import json, os, boto3

SF_ARN = os.environ["STATE_MACHINE_ARN"]
sf = boto3.client("stepfunctions")

def handler(event, context):
    """
    event esperado:
    {
      "symbols": ["BTCUSDT","ETHUSDT"],
      "start_ms": 1698796800000,   # opcional
      "end_ms":   1698883200000    # opcional
    }
    """
    body = event if isinstance(event, dict) else {}
    symbols = body.get("symbols") or []
    if not symbols:
        return {"statusCode": 400, "body": json.dumps({"error": "symbols required"})}

    # la entrada del State Machine puede conservar start/end para que el Map los pase a cada worker
    input_obj = {"symbols": [{"symbol": s, "start_ms": body.get("start_ms"), "end_ms": body.get("end_ms")} for s in symbols]}

    res = sf.start_execution(
        stateMachineArn=SF_ARN,
        input=json.dumps(input_obj)
    )
    return {"statusCode": 202, "body": json.dumps({"executionArn": res["executionArn"]})}