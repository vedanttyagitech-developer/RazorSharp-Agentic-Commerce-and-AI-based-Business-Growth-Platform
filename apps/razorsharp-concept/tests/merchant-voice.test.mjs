import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
// The merchant composer once hid its microphone behind a merchant-only hint, so voice
// could never connect from Merchant Command -- and a stale project-tour flag diverted
// every typed operations question into the tour narration instead of the copilot.
function harness(props={}, session={}){
 const store=new Map(Object.entries(session));
 const states=[],refs=[],effects=[],listeners=new Map();let slot=0,rs=0,es=0,tree;const effectDeps=[];
 const voice={phase:'idle',live:false,reply:null,notice:null,transcript:'',speech:'',spokenWords:0,spoken:[],starts:0,interrupt(){},say(text){this.spoken.push(text);return true},startListening(){this.starts++},reset(){},finishListening(){}}; 
 const react={useState(v){const i=slot++;if(!(i in states))states[i]=typeof v==='function'?v():v;return [states[i],n=>states[i]=typeof n==='function'?n(states[i]):n]},useRef(v){return refs[rs++]??={current:v}},useEffect(f,deps){const i=es++;const old=effectDeps[i];if(!old||!deps||deps.some((v,j)=>v!==old[j]))effects.push(f);effectDeps[i]=deps}};
 const doc={activeElement:null,addEventListener:(n,f)=>listeners.set(n,f),removeEventListener:n=>listeners.delete(n)};
 const jsx=(type,props)=>({type,props:props??{}});const exports={};
 const sessionStorage={setItem(k,v){store.set(k,String(v))},getItem(k){return store.has(k)?store.get(k):null},removeItem(k){store.delete(k)}};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/concept.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,sessionStorage,window:{addEventListener(){},removeEventListener(){}},document:doc,require(n){if(n==='react')return react;if(n.includes('continuity'))return {transition:fn=>fn()};if(n==='react/jsx-runtime')return {jsx,jsxs:jsx};if(n.includes('voice-session'))return {useVoiceSession:()=>({...voice,say:voice.say.bind(voice),startListening:voice.startListening.bind(voice),reset:voice.reset.bind(voice),finishListening:voice.finishListening.bind(voice),interrupt:voice.interrupt.bind(voice)})};if(n.includes('response-motion'))return {responseLabels:{thinking:'Thinking'}};return {}}});
 const nodes=n=>!n||typeof n!=='object'?[]:Array.isArray(n)?n.flatMap(nodes):[n,...nodes(n.props?.children)];
 const input=()=>nodes(tree).find(n=>n.type==='textarea');
 const inside={};
 function draw(){slot=rs=es=0;tree=exports.Composer({surface:'merchant',keepExpanded:true,onSend(){},...props});input().props.ref.current={blur(){},focus(){}};for(const f of effects.splice(0))f();return tree}
 draw();return {draw,input,props,voice,inside,doc,store,mic:()=>nodes(tree).find(n=>n.props?.['aria-label']==='Start voice conversation'),type:(text)=>{input().props.onChange({target:{value:text}});draw()},submit:()=>tree.props.onSubmit({preventDefault(){}})};
}
test('merchant composer offers a voice entry point',()=>{const h=harness();assert.ok(h.mic(),'mic button missing on the merchant surface')});
test('typed merchant text reaches the operations copilot when voice is not live',()=>{const sent=[];const h=harness({onSend:text=>sent.push(text)},{'razorsharp:tour':'on'});h.type('Which products need restocking?');h.submit();assert.deepEqual(sent,['Which products need restocking?']);assert.deepEqual(h.voice.spoken,[])});
test('typed merchant text retains the merchant response handler while voice is live',()=>{const sent=[];const h=harness({onSend:text=>sent.push(text)});h.voice.live=true;h.draw();h.type('Summarise approvals');h.submit();assert.deepEqual(h.voice.spoken,[]);assert.deepEqual(sent,['Summarise approvals'])});
test('merchant mic press uses the provided voice handler',()=>{let mic=0;const h=harness({onStartVoice:()=>mic++});h.mic().props.onClick();assert.equal(mic,1)});
