"""Only fake keys / HTTP doubles. No network and no real provider qualification."""

import asyncio
import json
from dataclasses import asdict, replace

import httpx
import pytest
from test_api import submit_body
from test_deepseek import envelope
from test_qualification import environment, finish, start
from test_table_plan import plan

from werewolf import deepseek, siliconflow
from werewolf.deepseek import ProviderError
from werewolf.demo import simulated_action
from werewolf.profiles import GenerationParams, ModelConnection, SeatProfile, canonical_json
from werewolf.prompts import PromptBuilder
from werewolf.providers import adapter_version, build_adapter, preflight
from werewolf.qualification_questions import prepare_question, questions
from werewolf.rulebook import Rulebook
from werewolf.siliconflow import HUNYUAN_MODEL, OFFICIAL_URL, SiliconFlowAdapter


def decision(**generation):
    connection = ModelConnection(id="sf", provider="siliconflow", model=HUNYUAN_MODEL,
                                 base_url=OFFICIAL_URL, adapter_version=adapter_version("siliconflow"),
                                 generation=GenerationParams(**generation))
    return prepare_question(PromptBuilder(Rulebook.load()), SeatProfile(seat=1, connection_id="sf"),
                            connection, questions()[0])


@pytest.mark.asyncio
async def test_hy4_exact_endpoint_parameters_identity_and_usage(monkeypatch):
    sent = []
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid-proxy:9999")

    def handler(request):
        assert str(request.url) == OFFICIAL_URL + "/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-sf-key"
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == HUNYUAN_MODEL
        assert body["enable_thinking"] is False and body["stream"] is False
        assert "thinking" not in body and body["thinking_budget"] == 512
        assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 256
        assert "fake-sf-key" not in json.dumps(body) and len(body["messages"]) == 2
        return httpx.Response(200, json=envelope(usage={"prompt_tokens": 100, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 60}, "completion_tokens_details": {"reasoning_tokens": 3}}))

    response = await SiliconFlowAdapter("fake-sf-key", transport=httpx.MockTransport(handler), simulated=True).complete(decision())
    assert len(sent) == 1 and response.simulated
    assert asdict(response.usage) == {"input_tokens": 100, "output_tokens": 10,
                                    "cached_input_tokens": 60, "reasoning_tokens": 3}
    assert "reasoning-canary" not in repr(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [(400,"request_parameters",False), (401,"authentication",False),
    (402,"balance",False), (403,"authentication",False), (422,"request_parameters",False),
    (429,"rate_limit",True), (503,"server_error",True), (302,"http_error",False)])
async def test_errors_never_switch_models_retry_silently_follow_redirects_or_expose_secrets(status, code, retryable):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(status, text="secret-canary", headers={"Location": "https://evil.example"})

    with pytest.raises(ProviderError) as error:
        await SiliconFlowAdapter("fake-sf-key", transport=httpx.MockTransport(handler)).complete(decision())
    assert calls == [HUNYUAN_MODEL] and error.value.code == code and error.value.retryable is retryable
    assert error.value.usage.input_tokens is None and "secret-canary" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [("timeout","timeout"), ("missing_usage",None),
    ("truncated","truncated_response"), ("json","invalid_envelope"), ("oversized","oversized_response")])
async def test_unknown_usage_and_bounded_envelopes(case, code):
    def handler(request):
        if case == "timeout":
            raise httpx.ReadTimeout("secret-canary", request=request)
        if case in {"json", "oversized"}:
            return httpx.Response(200, content=b"x" * (1_000_001 if case == "oversized" else 20))
        body = envelope(usage=None)
        if case == "truncated":
            body["usage"] = {"prompt_tokens": 88, "completion_tokens": 7}
            body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)
    adapter = SiliconFlowAdapter("fake-sf-key", transport=httpx.MockTransport(handler))
    if code:
        with pytest.raises(ProviderError) as error:
            await adapter.complete(decision())
        assert error.value.code == code and "secret-canary" not in str(error.value)
        assert error.value.usage.input_tokens == (88 if case == "truncated" else None)
    else:
        assert (await adapter.complete(decision())).usage.input_tokens is None


def test_capability_checks_and_provider_versions_do_not_reuse_deepseek_policy():
    request = decision()
    assert adapter_version("deepseek") == deepseek.ADAPTER_VERSION
    assert request.connection.adapter_version != deepseek.ADAPTER_VERSION
    assert isinstance(build_adapter(request.connection, "fake"), SiliconFlowAdapter)
    for connection, code in [
        (request.connection.model_copy(update={"base_url":"https://evil.example"}), "unsupported_connection"),
        (request.connection.model_copy(update={"model":"tencent/Other"}), "unsupported_model"),
        (request.connection.model_copy(update={"provider":"dashscope"}), "unsupported_connection"),
    ]:
        with pytest.raises(ProviderError, match=code):
            preflight(replace(request, connection=connection))
    for params, code in [({"reasoning_budget":128}, "unsupported_reasoning_budget"),
                         ({"max_output_tokens":65537}, "invalid_output_limit")]:
        with pytest.raises(ProviderError, match=code):
            preflight(decision(**params))


async def save_sf(client, headers):
    response = await client.post("/api/account/connections", headers=headers,
        json={"label":"fake Hunyuan", "provider":"siliconflow", "model":HUNYUAN_MODEL,
              "api_key":"fake-sf-key"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_preview_is_read_only_and_default_exam_routes_to_sf_without_real_grant(tmp_path, monkeypatch):
    sent = []
    original = SiliconFlowAdapter

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        state = json.loads(body["messages"][-1]["content"])
        from test_qualification import correct
        q = next(q for q in questions(state["state"]["rules"]) if q.id == state["qualification_task"]["id"])
        return httpx.Response(200, json=envelope(canonical_json(correct(q))))

    monkeypatch.setattr(siliconflow, "SiliconFlowAdapter", lambda key: original(
        key, transport=httpx.MockTransport(handler), simulated=True))
    async with environment(tmp_path) as (app, client, _user, headers, _cid, _):
        app.state.qualifications.factory = None  # Exercise production provider dispatch with offline transport.
        sf = await save_sf(client, headers)
        before = (await client.post("/api/account/qualifications/preview", headers=headers,
                                   json={"connection_id":sf})).json()
        assert not sent and before["unsupported"] is None and before["thinking_mode"] == "requested_disabled"
        run, _ = await start(client, headers, sf)
        report = await finish(app, client, run)
        assert report["status"] == "passed" and report["simulated"] and not report["qualified"]
        assert len(sent) == report["usage"]["calls"] == 12
        assert all(a["provider"] == "siliconflow" and a["model"] == HUNYUAN_MODEL for a in report["attempts"])
        assert "fake-sf-key" not in repr(report)
        await client.get(f"/api/account/qualifications/{run['id']}")
        assert len(sent) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("players", [6, 9])
async def test_mixed_game_routes_each_frozen_seat_and_keeps_exam_game_usage_separate(tmp_path, monkeypatch, players):
    sent = []
    originals = {"deepseek":deepseek.DeepSeekAdapter, "siliconflow":SiliconFlowAdapter}

    def factory(provider):
        def handler(request):
            body = json.loads(request.content)
            assert request.url.host == ("api.deepseek.com" if provider == "deepseek" else "api.siliconflow.cn")
            assert body["model"] == ("fixed-chosen-model" if provider == "deepseek" else HUNYUAN_MODEL)
            state = json.loads(body["messages"][-1]["content"])["state"]
            sent.append((provider, state["seat"]))
            spec = state["legal_actions"][0]
            action = asdict(simulated_action(state))
            fields = {"kind"}
            if "max_length" in spec:
                fields.add("text")
            if "targets" in spec or spec["kind"] == "witch":
                fields.add("target")
            if spec["kind"] == "witch":
                fields.add("potion")
            return httpx.Response(200, json=envelope(canonical_json({k:v for k,v in action.items() if k in fields})))
        return lambda key: originals[provider](key, transport=httpx.MockTransport(handler), simulated=True)

    monkeypatch.setattr(deepseek, "DeepSeekAdapter", factory("deepseek"))
    monkeypatch.setattr(siliconflow, "SiliconFlowAdapter", factory("siliconflow"))
    async with environment(tmp_path, reading_scale=0, game_retry_delay=0) as (app, client, _user, headers, cid, exam):
        sf = await save_sf(client, headers)
        roster = {"players":players,"wolves":2 if players == 6 else 3,"hunters":0 if players == 6 else 1}
        shared = plan(sf)
        shared["roster"] = roster
        shared["ai_profiles"] = [{**shared["ai_profiles"][0],"seat":s} for s in range(2,players + 1)]
        batch = (await client.post("/api/account/qualification-batches/preview", headers=headers, json=shared)).json()
        assert len(batch["groups"]) == 1 and len(batch["groups"][0]["seats"]) == players - 1
        assert batch["normal_calls"] == (12 if players == 6 else 13) and not sent
        # Synthetic test grants from an explicitly injected local exam double, never real credentials.
        for connection_id in [cid, sf]:
            preview = (await client.post("/api/account/qualifications/preview", headers=headers,
                json={"connection_id":connection_id,"roster":roster})).json()
            run = (await client.post("/api/account/qualifications", headers=headers, json={
                "connection_id":connection_id,"roster":roster,"operation_id":connection_id,
                "confirm_cost":True,"expected_version":preview["configuration_version"]})).json()
            assert (await finish(app, client, run))["qualified"]
        cached = (await client.post("/api/account/qualification-batches/preview", headers=headers, json=shared)).json()
        assert cached["normal_calls"] == 0 and cached["groups"][0]["cached"]
        table = plan(cid)
        table["roster"] = roster
        table["ai_profiles"] = [{**table["ai_profiles"][0], "seat":s} for s in range(2,players + 1)]
        table["playback_mode"] = "auto"
        for p in table["ai_profiles"]:
            if p["seat"] % 2 == 0:
                p["connection_id"] = sf
        admission = (await client.post("/api/account/table-plan/admission", headers=headers, json=table)).json()
        assert admission["all_qualified"] and not sent
        response = await client.post("/api/account/games", headers=headers, json={"plan":table,
            "operation_id":"mixed", "confirm_cost":True,
            "expected_versions":{str(s["seat"]):s["configuration_version"] for s in admission["seats"]}})
        assert response.status_code == 201, response.text
        gid = response.json()["game"]["game_id"]
        for _ in range(100):
            async with asyncio.timeout(5):
                while True:
                    state = (await client.get(f"/api/games/{gid}")).json()
                    if state["status"] != "running" or state["turn_version"]:
                        break
                    await asyncio.sleep(.001)
            if state["status"] == "finished":
                break
            assert state["status"] == "running", state
            assert (await client.post(f"/api/games/{gid}/actions", json=submit_body(state))).status_code == 200
        else:
            pytest.fail("mixed offline game did not finish")
        assert {p for p,_ in sent} == {"siliconflow", "deepseek"}
        assert all(p == ("siliconflow" if seat % 2 == 0 else "deepseek") for p,seat in sent)
        count = len(sent)
        replay = (await client.get(f"/api/games/{gid}/replay")).json()
        await client.get(f"/api/games/{gid}/stream?follow=false")
        await client.get(f"/api/games/{gid}")
        summary = (await client.get(f"/api/games/{gid}/usage")).json()
        assert len(sent) == count == summary["calls"] == len(replay["usage"])
        assert len(exam.calls) == (24 if players == 6 else 26)
        assert summary["known_input_tokens"] == count * 100
        assert {r["provider"] for r in replay["usage"]} == {"deepseek", "siliconflow"}
