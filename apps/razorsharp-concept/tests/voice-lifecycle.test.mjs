import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(file,extra={}) {
 const exports={};
 const code=ts.transpileModule(readFileSync(new URL('../'+file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,DOMException,AbortController,ArrayBuffer,Int16Array,Float32Array,console,setTimeout,clearTimeout,...extra,require:(name)=>name==='./wire'?load('lib/voice/wire.ts'):name==='./conversation'?load('lib/voice/conversation.ts'):(extra.require?.(name)??{})});
 return exports;
}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function audioFixture({permission,resume,worklet}={}) {
 let stopped=0,closed=0,started=0;
 const stream={getTracks:()=>[{stop:()=>stopped++}]};
 class Context {
  state=resume?'suspended':'running'; currentTime=0; destination={};
  audioWorklet={addModule:()=>worklet?.promise??Promise.resolve()};
  close(){closed++;this.state='closed';return Promise.resolve()}
  resume(){return resume?.promise??Promise.resolve()}
  createMediaStreamSource(){return {connect(){}}}
  createBuffer(_,size){return {duration:size/24000,getChannelData:()=>new Float32Array(size)}}
  createBufferSource(){return {connect(){},start(){started++},stop(){},onended:null}}
 }
 class Node {port={close(){}};disconnect(){}}
 const api=load('lib/voice/audio.ts',{AudioContext:Context,AudioWorkletNode:Node,navigator:{mediaDevices:{getUserMedia:()=>permission?.promise??Promise.resolve(stream)}}});
 return {...api,stream,counts:()=>({stopped,closed,started})};
}
test('Closing while microphone permission is pending releases a late stream',async()=>{
 const permission=deferred();const f=audioFixture({permission});const mic=new f.Microphone();
 const opening=mic.start({sampleRateHz:16000,frameMs:100},()=>assert.fail('No audio after stop'));
 const rejected=assert.rejects(opening,{name:'AbortError'});
 await tick();await mic.stop();permission.resolve(f.stream);await rejected;
 assert.equal(f.counts().stopped,1);assert.equal(f.counts().closed,1);
});
test('Microphone worklet failure releases device and audio context',async()=>{
 const worklet={promise:Promise.resolve().then(()=>{throw Error('worklet unavailable')})};
 // Attach the handler immediately; the microphone will await the same rejected promise.
 worklet.promise.catch(()=>{});
 const f=audioFixture({worklet});const mic=new f.Microphone();
 await assert.rejects(mic.start({sampleRateHz:16000,frameMs:100},()=>{}),/worklet unavailable/);
 assert.equal(f.counts().stopped,1);assert.equal(f.counts().closed,1);
});
for(const ending of ['flush','close'])test(`Pending audio resume cannot play after ${ending}`,async()=>{
 const resume=deferred();const f=audioFixture({resume});const player=new f.SpeechPlayer(24000,()=>assert.fail('Cancelled audio must not announce playback ended'));
 const playing=player.play(new ArrayBuffer(4));await tick();await player[ending]();resume.resolve();await playing;
 assert.equal(f.counts().started,0);
});
test('Odd-length PCM is rejected before scheduling audio',async()=>{
 const f=audioFixture();const player=new f.SpeechPlayer(24000,()=>{});
 await assert.rejects(player.play(new ArrayBuffer(3)),/Invalid PCM16/);assert.equal(f.counts().started,0);
});
test('A late ticket cannot reopen a closed voice client',async()=>{
 const ticket=deferred();let sockets=0;
 const {VoiceClient}=load('lib/voice/client.ts',{fetch:()=>ticket.promise,WebSocket:class{constructor(){sockets++}},require:()=>({Microphone:class{async stop(){}},SpeechPlayer:class{}})});
 const client=new VoiceClient();const opening=client.open();const rejected=assert.rejects(opening,{name:'AbortError'});
 await client.close();ticket.resolve({ok:true,json:async()=>({ticket:'test',socket_url:'ws://local'})});await rejected;assert.equal(sockets,0);
});
test('Malformed audio length does not reach the player',async()=>{
 const {VoiceClient}=load('lib/voice/client.ts',{require:()=>({Microphone:class{},SpeechPlayer:class{}})});
 const client=new VoiceClient();let plays=0;client.player={play:()=>plays++};client.pendingChunk={seq:0,byte_length:8};
 await assert.rejects(client.receive({data:new ArrayBuffer(4)}),/announced PCM16/);assert.equal(plays,0);
});
test('Interrupted generations cannot redisplay products or release a new echo gate',async()=>{
 const sent=[],replies=[];
 const {VoiceClient}=load('lib/voice/client.ts',{WebSocket:{OPEN:1},require:()=>({Microphone:class{},SpeechPlayer:class{}})});
 const client=new VoiceClient({onReply:text=>replies.push(text)});
 client.socket={readyState:1,send:text=>sent.push(JSON.parse(text))};client.player={flush(){}};
 await client.receive({data:JSON.stringify({type:'speech_start',utterance_id:1,speech_generation:1})});
 client.bargeIn();
 await client.receive({data:JSON.stringify({type:'agent_reply',text:'stale',speech_generation:1})});
 await client.receive({data:JSON.stringify({type:'interrupted',speech_generation:2})});
 await client.receive({data:JSON.stringify({type:'speech_end',speech_generation:1})});
 assert.equal(replies.length,0);assert.equal(sent.filter(x=>x.type==='playback_ended').length,0);
 await client.receive({data:JSON.stringify({type:'agent_reply',text:'current',speech_generation:2})});
 assert.deepEqual(replies,['current']);
});
test('Typed input reports a disconnected socket instead of silently accepting the turn',()=>{
 const {VoiceClient}=load('lib/voice/client.ts',{WebSocket:{OPEN:1},require:()=>({Microphone:class{},SpeechPlayer:class{}})});
 const client=new VoiceClient();assert.equal(client.text('Show bread'),false);
 let sent=0;client.socket={readyState:1,send(){sent++}};
 assert.equal(client.text('Show bread'),true);assert.equal(sent,1);
});

test('Physical microphone disconnect releases capture and reports unavailable once',async()=>{
 const permission=deferred();const f=audioFixture({permission});const notices=[];
 let stopped=0;const track={stop:()=>stopped++,onended:null};
 const mic=new f.Microphone();const starting=mic.start({sampleRateHz:16000,frameMs:100},()=>{},message=>notices.push(message));
 permission.resolve({getTracks:()=>[track]});await starting;
 const ended=track.onended;ended();ended();await tick();
 assert.equal(stopped,1);assert.equal(notices.length,1);assert.match(notices[0],/disconnected/);
 assert.equal(f.counts().closed,1);
});

test('Voice output is unlocked before waiting for the ticket and closed on cancellation',async()=>{
 const ticket=deferred();let resumed=0,closed=0;
 const {VoiceClient}=load('lib/voice/client.ts',{fetch:()=>ticket.promise,AudioContext:class{resume(){resumed++;return Promise.resolve()}close(){closed++;return Promise.resolve()}},require:()=>({Microphone:class{async stop(){}}})});
 const client=new VoiceClient();const opening=client.open();const rejected=assert.rejects(opening,{name:'AbortError'});
 assert.equal(resumed,1,'Unlock must happen in the original click task');
 await client.close();assert.equal(closed,1);
 ticket.resolve({ok:true,json:async()=>({ticket:'fixture',socket_url:'ws://local'})});await rejected;
});


test('Every spoken turn acknowledges playback even when the generation stays unchanged',async()=>{
 const sent=[];
 const {VoiceClient}=load('lib/voice/client.ts',{WebSocket:{OPEN:1},require:()=>({Microphone:class{},SpeechPlayer:class{}})});
 const client=new VoiceClient();
 client.socket={readyState:1,send:text=>sent.push(JSON.parse(text))};
 for(let turn=0;turn<3;turn++){
  await client.receive({data:JSON.stringify({type:'speech_start',utterance_id:turn+1,speech_generation:0})});
  await client.receive({data:JSON.stringify({type:'speech_chunk',utterance_id:turn+1,speech_generation:0,seq:turn,byte_length:4})});
  await client.receive({data:JSON.stringify({type:'speech_end',utterance_id:turn+1,speech_generation:0})});
  assert.equal(sent.length,turn,'Server completion alone must not acknowledge queued audio');
  client.outputs.get(turn+1).audible=true;
  client.playbackEnded(turn+1);
  client.playbackEnded(turn+1);
  assert.equal(sent.length,turn+1,'One completion per spoken turn');
  assert.equal(sent[turn].type,'playback_ended');
 }
});

test('Automatic barge-in requires sustained speech, ignoring silence and clicks',()=>{
 const {BargeInDetector}=load('lib/voice/client.ts');
 const gate=new BargeInDetector();
 const frame=level=>new Int16Array(1600).fill(level).buffer;
 assert.equal(gate.observe(frame(0),true,16000),null);
 assert.equal(gate.observe(frame(8000),true,16000),null);
 assert.equal(gate.observe(frame(0),true,16000),null);
 assert.equal(gate.observe(frame(8000),true,16000),null);
 assert.equal(gate.observe(frame(8000),true,16000),null);
 assert.equal(gate.observe(frame(8000),true,16000).length,3);
 assert.equal(gate.observe(frame(8000),false,16000),null);
});

test('Microphone barge-in flushes playback before sending preserved speech onset',()=>{
 const {VoiceClient}=load('lib/voice/client.ts',{WebSocket:{OPEN:1},require:()=>({Microphone:class{}})});
 const sent=[];let flushes=0;
 const client=new VoiceClient();client.socket={readyState:1,bufferedAmount:0,send:frame=>sent.push(frame)};
 client.player={flush:()=>flushes++};client.speechActive=true;client.recognitionReady=true;
 const pcm=new Int16Array(1600).fill(8000).buffer;
 client.sendAudio(pcm);client.sendAudio(pcm);client.sendAudio(pcm);
 assert.equal(flushes,1);
 assert.equal(JSON.parse(sent[2]).type,'barge_in');
 assert.equal(sent.slice(3).length,3);
 assert.equal(client.speechActive,false);
 assert.equal(client.pendingInterrupts,1);
});

 test('Microphone does not transmit until recognition is ready',async()=>{
 const {VoiceClient}=load('lib/voice/client.ts',{WebSocket:{OPEN:1},require:()=>({Microphone:class{}})});
 const sent=[];const client=new VoiceClient();client.socket={readyState:1,bufferedAmount:0,send:f=>sent.push(f)};
 const pcm=new Int16Array(1600).fill(100).buffer;
 client.sendAudio(pcm);assert.equal(sent.length,0);
 await client.receive({data:JSON.stringify({type:'recognition_state',state:'ready'})});
 client.sendAudio(pcm);assert.equal(sent.length,1);
 await client.receive({data:JSON.stringify({type:'recognition_state',state:'unavailable'})});
 client.sendAudio(pcm);assert.equal(sent.length,1);
 });
 test('Queued audio keeps the composer working until audible playback begins',()=>{
 const {initialConversation,conversationTransition,conversationPhase}=load('lib/voice/conversation.ts');
 let state=initialConversation();
 for(const event of [{type:'ready'},{type:'capture',active:true},{type:'audio_queued',utterance_id:1}])state=conversationTransition(state,event);
 assert.equal(conversationPhase(state),'transcribing');
 state=conversationTransition(state,{type:'audio_started',utterance_id:1});assert.equal(conversationPhase(state),'speaking');
 state=conversationTransition(state,{type:'audio_finished',utterance_id:1});assert.equal(conversationPhase(state),'listening');
 });
