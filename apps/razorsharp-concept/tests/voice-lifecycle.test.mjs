import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(file,extra={}) {
 const exports={};
 const code=ts.transpileModule(readFileSync(new URL('../'+file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,DOMException,AbortController,ArrayBuffer,Int16Array,Float32Array,console,setTimeout,clearTimeout,require:()=>({}),...extra});
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
 await client.receive({data:JSON.stringify({type:'speech_start',speech_generation:1})});
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
