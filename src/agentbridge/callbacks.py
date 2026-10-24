from urllib.parse import urlsplit

import httpx

from .errors import BridgeError


def _post(spec, name, body, suffix=''):
    url = spec.callbacks.get(name, "")
    target = urlsplit(url)
    expected = "/internal/agentbridge/actions/" + name
    if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1", "::1"} or target.path != expected or target.username or target.query or target.fragment:
        raise BridgeError("callback_not_allowed", "业务回调地址未获授权")
    if spec.callback_token is None:
        raise BridgeError("callback_not_allowed", "缺少业务回调凭据")
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
        result = client.post(url + suffix, json=body, headers={"Authorization": "Bearer " + spec.callback_token.get_secret_value()})
        if result.status_code != 200:
            raise BridgeError("callback_failed", "业务操作未通过宿主校验")
        return result.json()


def invoke(spec, name, arguments, meta):
    return _post(spec, name, {"task_id": spec.task_id, "execution_id": spec.execution_id, "node_id": spec.node_id,
                 "control_revision": spec.control_revision, "op_id": meta.op_id, "payload": arguments})


def ack(spec, name, op_id, receipt):
    return _post(spec, name, {"task_id": spec.task_id, "execution_id": spec.execution_id, "node_id": spec.node_id,
                 "control_revision": spec.control_revision, "op_id": op_id, "receipt": receipt}, '/ack')
