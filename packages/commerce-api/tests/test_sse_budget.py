from types import SimpleNamespace

import pytest
from commerce_api.routers.evidence import stream_budget
from commerce_domain.workload import WorkloadGate
from fastapi import HTTPException


def test_stream_limit_is_held_until_cleanup_and_shared_by_buyer():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workload=WorkloadGate())))
    ctx = SimpleNamespace(tenant_id="tenant", buyer_ref="buyer", principal=None)
    streams = [stream_budget(request, ctx) for _ in range(4)]
    for stream in streams:
        next(stream)
    with pytest.raises(HTTPException) as error:
        next(stream_budget(request, ctx))
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "60"
    streams.pop().close()
    replacement = stream_budget(request, ctx)
    next(replacement)
    replacement.close()
    for stream in streams:
        stream.close()


def test_tenant_limit_spans_buyers_and_cleans_up_on_failure():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workload=WorkloadGate())))
    streams = []
    for i in range(24):
        ctx = SimpleNamespace(tenant_id="tenant", buyer_ref=str(i), principal=None)
        stream = stream_budget(request, ctx)
        next(stream)
        streams.append(stream)
    ctx = SimpleNamespace(tenant_id="tenant", buyer_ref="next", principal=None)
    with pytest.raises(HTTPException):
        next(stream_budget(request, ctx))
    with pytest.raises(RuntimeError):
        streams.pop().throw(RuntimeError("disconnect"))
    replacement = stream_budget(request, ctx)
    next(replacement)
    replacement.close()
    for stream in streams:
        stream.close()
