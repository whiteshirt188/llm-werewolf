"""Actual public roster in player documents, with frozen previous documents untouched."""

import json
from itertools import combinations

import pytest

from werewolf.engine import GameEngine
from werewolf.player_state import PLAYER_PROTOCOL
from werewolf.profiles import ModelConnection, SeatProfile
from werewolf.prompts import PromptBuilder
from werewolf.roster import RosterConfig
from werewolf.rulebook import Rulebook

ROSTERS = [RosterConfig(), RosterConfig(witches=0)] + [
    RosterConfig(players=players, wolves=2 if players < 9 else 3,
                 **{role: int(role in pair) for role in ('witches', 'hunters', 'guards', 'knights')})
    for players in range(6, 10)
    for pair in combinations(('witches', 'hunters', 'guards', 'knights'), 2)
]


@pytest.mark.parametrize('roster', ROSTERS)
def test_all_supported_role_combinations_render_only_present_mechanics(roster):
    book = Rulebook.load()
    rules = roster.engine_config().public_rules()
    text = dict(book.select('villager', 'speak', rules))['规则.md']
    assert len(text) <= 1000 and '{本局配置}' not in text and '<!--' not in text
    assert f'本局{rules["players"]}人' in text
    for role, key, label in book.ROLE_COUNTS:
        assert (f'{rules[key]}名{label}' in text) is bool(rules[key])
    body = text.split('## 信息', 1)[1]
    for role, key, label in book.ROLE_COUNTS[1:-1]:
        assert (label in body) is bool(rules[key]), role
    if not rules['witches']:
        assert '毒杀' not in body and '救守' not in body


def test_roster_and_disabled_skills_are_in_both_opening_and_action_requests():
    game = GameEngine(38, RosterConfig(players=9, wolves=3, hunters=1, last_words=False).engine_config())
    seat = game.next_actor()
    connection = ModelConnection(id='m', provider='mock', model='fixed', base_url='mock://local')
    profile = SeatProfile(seat=seat, connection_id='m')
    builder = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL)
    for request in (builder.build_opening(profile, connection, game.player_view(seat)),
                    builder.build(profile, connection, game.player_view(seat))):
        system = request.messages[0].content
        payload = json.loads(request.messages[1].content)
        assert payload['state']['rules']['guards'] == payload['state']['rules']['knights'] == 0
        assert '本局9人：3名狼人、1名预言家、1名女巫、1名猎人、3名村民。' in system
        assert '没有守卫、骑士，这些身份的技能不会发生。遗言关闭。' in system
        assert '守卫可自守' not in system and '骑士决斗全局' not in system
        assert '尚未出现的发言不能当依据' in system
        assert all(set(p) == {'seat', 'alive'} for p in payload['state']['players'])


def test_previous_v9_frozen_document_is_not_rewritten_by_new_renderer():
    current = Rulebook.load()
    previous = Rulebook(tuple((('规则.md', '旧正文：{胜负规则}。守卫规则保持原文。') if name == 'rules-v2/规则.md'
                              else (name, text)) for name, text in current.files))
    text = dict(previous.select('villager', 'speak', RosterConfig().engine_config().public_rules()))['规则.md']
    assert text == '旧正文：狼人全灭则好人胜；村民全灭或神职全灭则狼人胜。。守卫规则保持原文。'
    assert '本局6人' not in text
