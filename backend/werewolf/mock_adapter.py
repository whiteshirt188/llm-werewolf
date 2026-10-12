"""Offline transport double. Uses exactly the context sent to the model."""

import json
from dataclasses import asdict

from .agent import ModelResponse
from .demo import simulated_action
from .opening import OPENING_PROTOCOL
from .player_state import PLAYER_PROTOCOL
from .profiles import canonical_json
from .prompts import DecisionRequest


class MockAdapter:
    async def complete(self, request: DecisionRequest) -> ModelResponse:
        state = json.loads(request.messages[-1].content)["state"]
        if request.protocol == OPENING_PROTOCOL:
            return ModelResponse(canonical_json({"intent": "先观察大家的反应，再决定怎么打"}), simulated=True)
        action = simulated_action(state)
        schema = json.loads(request.response_schema)
        required = (schema["properties"]["action"] if request.protocol == PLAYER_PROTOCOL else schema)["required"]
        data = {key: value for key, value in asdict(action).items() if key in required}
        if request.protocol == PLAYER_PROTOCOL:
            data = {"action": data, "player_update": {}}
        # No token guesses; offline results are always explicitly simulated.
        return ModelResponse(canonical_json(data), simulated=True)
