import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {chatMessages,waitingText} from '../src/chat.ts';
import {makeAction,describeEvent,defaultPersona} from '../src/game.ts';
import {ceremonies} from '../src/ceremony.ts';
import {diversifyStrategies,emptyPlan,planBody,applyConnection,realStartBody} from '../src/table-plan.ts';
import type {Admission} from '../src/table-plan.ts';
import {sixRoster} from '../src/roster.ts';
import {initialTyping,syncTyping,bubbleReady} from '../src/typing.ts';
import type {GameView} from '../src/types.ts';

test('last words accept explicit skip, reject overflow, and leave normal speech nonempty',()=>{
  const spec={kind:'last_words' as const,max_length:300,allow_empty:true};
  assert.deepEqual(makeAction(spec,'',''),{kind:'last_words',text:''});
  assert.deepEqual(makeAction(spec,'','我的查验'),{kind:'last_words',text:'我的查验'});
  assert.throws(()=>makeAction(spec,'','字'.repeat(301)));
  assert.throws(()=>makeAction({kind:'speak',max_length:300},'',''));
  assert.match(describeEvent({kind:'last_words',day:1,data:{seat:3,text:''}}).text,/放弃遗言/);
});
test('dead-player last words stay public avatar bubbles and type one by one before day ceremony',()=>{
  const g={game_id:'words',mode:'mock',status:'running',human_seat:1,pause_reason:null,turn_version:'words',seat_profiles:[],view:{seat:1,players:[],legal_actions:[],checks:[],wolf_teammates:[],winner:null,alive:false,role:'seer',day:1,phase:'last_words',events:[
    {kind:'dawn',day:1,data:{dead:[2,3]}},{kind:'last_words_started',day:1,data:{seats:[2,3]}},
    {kind:'last_words',day:1,data:{seat:2,text:'第一条遗言'}},{kind:'last_words',day:1,data:{seat:3,text:'第二条遗言'}},
  ]}} as GameView;
  assert.deepEqual(ceremonies(g).map(c=>c.kind),['dawn','result']);
  g.view.legal_actions=[{kind:'last_words',max_length:300,allow_empty:true}];
  assert.match(waitingText(g),/轮到你发表遗言/);
  const messages=chatMessages(g), words=messages.filter(m=>m.kind==='last_words');
  assert.deepEqual(words.map(m=>m.seat),[2,3]);
  assert.ok(words.every(m=>m.scope==='public'));
  const typing=syncTyping(initialTyping([]),messages,messages,1,false);
  assert.equal(typing.queue.length,2);
  assert.equal(bubbleReady(typing,words[1].key),false);
  g.view.events.push({kind:'day_started',day:1,data:{knight_available:false}});
  assert.equal(ceremonies(g).filter(c=>c.kind==='speech').length,1);
});
test('strategy diversity is explicit and preserves models and speaking personas with correct serialization',()=>{
  const original=applyConnection(emptyPlan(),'same-api');
  const varied=diversifyStrategies(original);
  assert.equal(new Set(varied.ai_profiles.map(p=>p.persona.strategy)).size,5);
  assert.ok(original.ai_profiles.every(p=>p.persona.strategy==='balanced'));
  assert.ok(varied.ai_profiles.every(p=>p.connection_id==='same-api' && p.persona.description===defaultPersona().description));
  assert.deepEqual(planBody(varied,'').ai_profiles.map(p=>p.persona.strategy),varied.ai_profiles.map(p=>p.persona.strategy));
});
test('last-words defaults normalize for admission and explicit disabled rules cannot use enabled qualification',()=>{
  const plan={...applyConnection(emptyPlan(),'shared'),roster:sixRoster()};
  const admission={all_qualified:true,game_start_enabled:true,roster:{...sixRoster(),last_words:true},seats:plan.ai_profiles.map(p=>({seat:p.seat,connection_id:'shared',qualified:true,configuration_version:'v'}))} as Admission;
  assert.ok(realStartBody(plan,'',admission,'op',true));
  assert.throws(()=>realStartBody({...plan,roster:{...sixRoster(),last_words:false}},'',admission,'op',true));
  assert.equal(planBody({...plan,roster:{...sixRoster(),last_words:false}},'').roster?.last_words,false);
});
