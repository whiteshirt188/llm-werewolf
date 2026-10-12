import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {ceremonies} from '../src/ceremony.ts';
import {describeEvent,makeAction,rosterText,isPrivate,roles} from '../src/game.ts';
import {nineRoster,rosterError,ruleConfig,villagers} from '../src/roster.ts';
import {waitingText,presentationPhase} from '../src/chat.ts';
import type {GameEvent,GameView} from '../src/types.ts';

function game(events:GameEvent[]):GameView {
  return {game_id:'gods',mode:'mock',status:'running',human_seat:1,seat_profiles:[],pause_reason:null,turn_version:'shoot',
    view:{seat:1,role:'hunter',alive:false,day:1,phase:'hunter_shot',players:[],legal_actions:[{kind:'shoot',targets:[2,3],allow_abstain:true}],
      events,checks:[],wolf_teammates:[],winner:null}};
}
test('nine preset has mandatory seer and exactly three gods with three villagers',()=>{
  assert.equal(villagers(nineRoster()),3);
  for (const role of ['hunters','guards','knights'] as const) {
    const r={...nineRoster(),hunters:0,[role]:1};
    assert.equal(rosterError(r),'');
    assert.match(rosterText(ruleConfig(r)),new RegExp(roles[role==='hunters'?'hunter':role==='guards'?'guard':'knight']));
  }
  assert.ok(rosterError({...nineRoster(),seers:0}));
  assert.ok(rosterError({...nineRoster(),hunters:0}));
  assert.ok(rosterError({...nineRoster(),guards:1}));
});
test('new skill actions accept only legal targets and explicit skip',()=>{
  for (const kind of ['protect','shoot','duel'] as const) {
    const spec={kind,targets:[2,3],allow_abstain:true};
    assert.deepEqual(makeAction(spec,'2','irrelevant'),{kind,target:2});
    assert.deepEqual(makeAction(spec,'abstain',''),{kind,target:null});
    assert.throws(()=>makeAction(spec,'1',''));
    assert.throws(()=>makeAction(spec,'',''));
  }
});
test('public god events explain death, identity and skip without exposing private guard records',()=>{
  assert.equal(isPrivate({kind:'guard_action',day:1,data:{target:2}}),true);
  assert.match(describeEvent({kind:'hunter_ready',day:1,data:{seat:1}}).text,/完成后判定胜负/);
  assert.match(describeEvent({kind:'knight_duel',day:1,data:{seat:1,target:3,success:false}}).text,/骑士自己出局/);
  assert.match(describeEvent({kind:'hunter_shot',day:1,data:{seat:1,target:null}}).text,/放弃/);
  assert.match(waitingText(game([])),/轮到你/);
});
test('hunter window holds back speech ceremony, then day start precedes human speech',()=>{
  const g=game([{kind:'dawn',day:1,data:{dead:[1]}},{kind:'hunter_ready',day:1,data:{seat:1}}]);
  assert.deepEqual(ceremonies(g).map(c=>c.kind),['dawn','result']);
  g.view.events.push({kind:'hunter_shot',day:1,data:{seat:1,target:2}},{kind:'day_started',day:1,data:{}});
  assert.equal(ceremonies(g).filter(c=>c.kind==='speech').length,1);
  g.view.events.push({kind:'speak',day:1,data:{seat:3,text:'讨论'}});
  assert.equal(ceremonies(g).filter(c=>c.kind==='speech').length,1);
});
test('an unannounced knight window looks like speech, including older server views and own private controls',()=>{
  const g=game([{kind:'day_started',day:1,data:{knight_available:true}}]);
  g.view.phase='knight_duel'; g.view.alive=true; g.view.role='knight';
  g.view.legal_actions=[{kind:'duel',targets:[2,3],allow_abstain:true}];
  const before=structuredClone(g);
  assert.equal(presentationPhase(g),'day_speech');
  assert.deepEqual(g,before);
  assert.equal(ceremonies(g).some(c=>c.title.includes('决斗成功')),false);
  g.view.events.push({kind:'knight_duel',day:1,data:{seat:1,target:3,success:true}});
  assert.equal(ceremonies(g).some(c=>c.title.includes('决斗成功')),true);
});
