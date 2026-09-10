import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const exports={};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/voice/conversation.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports});
const {initialConversation,conversationTransition:fold,conversationPhase:phase}=exports;
const ready=()=>fold(fold(initialConversation(),{type:'connect'}),{type:'ready'});
test('Sending audio never labels the UI as Speaking before actual playback',()=>{
 let state=fold(ready(),{type:'audio_queued',utterance_id:1});
 assert.notEqual(phase(state),'speaking');
 state=fold(state,{type:'audio_started',utterance_id:1});assert.equal(phase(state),'speaking');
 state=fold(state,{type:'interrupt'});assert.notEqual(phase(state),'speaking');
});
test('Old intent completion cannot stop the current request',()=>{
 let state=fold(ready(),{type:'turn_opened',intent_id:1});
 state=fold(state,{type:'turn_opened',intent_id:2});state=fold(state,{type:'turn_reasoning',intent_id:2});
 state=fold(state,{type:'turn_closed',intent_id:1,outcome:'failed'});assert.equal(phase(state),'transcribing');
 state=fold(state,{type:'turn_closed',intent_id:2,outcome:'failed'});assert.equal(phase(state),'idle');
 assert.equal(fold(state,{type:'turn_reasoning',intent_id:2}).intent.stage,'closed');
});
test('Completion without TTS restores listening and a stale audio end cannot end newer audio',()=>{
 let state=fold(ready(),{type:'capture',active:true});state=fold(state,{type:'turn_opened',intent_id:1});
 state=fold(state,{type:'turn_closed',intent_id:1,outcome:'answered'});assert.equal(phase(state),'listening');
 state=fold(state,{type:'audio_queued',utterance_id:2});state=fold(state,{type:'audio_started',utterance_id:2});
 assert.equal(phase(fold(state,{type:'audio_finished',utterance_id:1})),'speaking');
 assert.equal(phase(fold(state,{type:'disconnect'})),'idle');
});
