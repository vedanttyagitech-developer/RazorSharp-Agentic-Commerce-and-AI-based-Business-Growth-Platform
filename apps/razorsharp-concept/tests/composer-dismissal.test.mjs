import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function harness(props={}){
 const states=[],refs=[],effects=[],listeners=new Map();let slot=0,rs=0,es=0,tree;const effectDeps=[];
 const voice={phase:'idle',live:false,reply:null,notice:null,spoken:[],starts:0,interrupt(){},say(text){this.spoken.push(text)},startListening(){this.starts++}};const Prompts=()=>null;
 const react={useState(v){const i=slot++;if(!(i in states))states[i]=typeof v==='function'?v():v;return [states[i],n=>states[i]=typeof n==='function'?n(states[i]):n]},useRef(v){return refs[rs++]??= {current:v}},useEffect(f,deps){const i=es++;const old=effectDeps[i];if(!old||deps.some((v,j)=>v!==old[j]))effects.push(f);effectDeps[i]=deps}};
 const doc={activeElement:null,addEventListener:(n,f)=>listeners.set(n,f),removeEventListener:n=>listeners.delete(n)};
 const jsx=(type,props)=>({type,props:props??{}});const exports={};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/concept.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,sessionStorage:{setItem(){},getItem(){return null}},window:{addEventListener(){},removeEventListener(){}},document:doc,require(n){if(n==='react')return react;if(n.includes('continuity'))return {transition:fn=>fn()};if(n==='react/jsx-runtime')return {jsx,jsxs:jsx};if(n.includes('voice-session'))return {useVoiceSession:()=>({...voice,say:voice.say.bind(voice),startListening:voice.startListening.bind(voice)})};if(n.includes('composer-prompts'))return {ComposerPrompts:Prompts};if(n.includes('response-motion'))return {responseLabels:{thinking:'Thinking'}};return {}}});
 const nodes=n=>!n||typeof n!=='object'?[]:Array.isArray(n)?n.flatMap(nodes):[n,...nodes(n.props?.children)];
 const input=()=>nodes(tree).find(n=>n.type==='textarea');
 const inside={};
 function draw(){slot=rs=es=0;tree=exports.Composer({onSend(){},...props});tree.props.ref.current={contains:target=>target===inside};input().props.ref.current={blur(){},focus(){}};for(const f of effects.splice(0))f();return tree}
 draw();return {draw,input,props,voice,inside,doc,buttons:()=>nodes(tree).filter(n=>n.type==='button').map(n=>n.props.children),click:label=>nodes(tree).find(n=>n.props?.['aria-label']===label).props.onClick(),submit:()=>tree.props.onSubmit({preventDefault(){}}),pick:question=>nodes(tree).find(n=>n.props?.onPick).props.onPick(question),fire:(event,target={})=>listeners.get(event)({target,type:event}),collapsed:()=>tree.props['data-collapsed'],prompts:()=>nodes(tree).filter(n=>n.type===Prompts).length};
}
test('orders and checkout default to collapsed with no suggestions',()=>{const h=harness();assert.equal(h.collapsed(),true);assert.equal(h.prompts(),0)});
test('home remains suggestion-free and outside touch collapses without losing draft',()=>{const h=harness({showSuggestions:true});assert.equal(h.collapsed(),false);assert.equal(h.prompts(),0);h.input().props.onFocus();h.input().props.onChange({target:{value:'two milk'}});h.draw();h.fire('pointerdown');h.draw();assert.equal(h.collapsed(),true);assert.equal(h.input().props.value,'two milk');h.input().props.onFocus();h.draw();assert.equal(h.collapsed(),false)});
test('outside scrolling collapses but scrolling inside composer does not',()=>{const h=harness();h.input().props.onFocus();h.draw();h.fire('scroll',h.inside);h.draw();assert.equal(h.collapsed(),false);h.fire('wheel');h.draw();assert.equal(h.collapsed(),true)});
test('dismissal never hides active voice or a running response',()=>{const h=harness();h.voice.live=true;h.voice.phase='listening';h.fire('touchmove');h.draw();assert.equal(h.collapsed(),false);h.voice.live=false;h.voice.phase='idle';h.props.phase='thinking';h.draw();assert.equal(h.collapsed(),false)});

test('focus-induced scrolling preserves the editor while a user wheel still dismisses it',()=>{const h=harness();h.input().props.onFocus();h.draw();h.doc.activeElement=h.inside;h.fire('scroll');h.draw();assert.equal(h.collapsed(),false);h.fire('wheel');h.draw();assert.equal(h.collapsed(),true)});

test('judge composer keeps suggestions visible after outside touch and scroll',()=>{const h=harness({judgeSuggestions:true});h.fire('pointerdown');h.fire('wheel');h.draw();assert.equal(h.collapsed(),false)});
test('suggestion starts voice or asks immediately without changing the draft',()=>{const h=harness({judgeSuggestions:true});h.pick('How does approval work?');assert.equal(h.voice.starts,1);assert.deepEqual(h.voice.spoken,[]);h.voice.live=true;h.pick('How does recovery work?');assert.deepEqual(h.voice.spoken,[]);assert.equal(h.voice.starts,1);assert.equal(h.input().props.value,'')});

test('judge microphone stays on the current surface and text reaches the project agent',()=>{let redirected=false;const h=harness({judgeSuggestions:true,onStartVoice(){redirected=true}});h.click('Start voice conversation');assert.equal(h.voice.starts,1);assert.equal(redirected,false);h.input().props.onChange({target:{value:'Explain the architecture'}});h.draw();h.submit();assert.deepEqual(h.voice.spoken,['Explain the architecture'])});
test('ordinary shopping stays expanded without judge suggestions and uses shopping handlers',()=>{const sent=[];let mic=0;const h=harness({keepExpanded:true,onSend:text=>sent.push(text),onStartVoice:()=>mic++});assert.equal(h.input().props.placeholder,'Or type what’s on your mind…');h.fire('wheel');h.draw();assert.equal(h.collapsed(),false);h.input().props.onChange({target:{value:'find milk'}});h.draw();h.submit();assert.deepEqual(sent,['find milk']);assert.deepEqual(h.voice.spoken,[]);h.click('Start voice conversation');assert.equal(mic,1)});

test("ordinary composer never offers a project tour",()=>{const h=harness({keepExpanded:true});assert.ok(!h.buttons().includes("Take a project tour"))});

test('rapid suggestion clicks cannot interrupt or replace the active request',()=>{const h=harness({judgeSuggestions:true});h.voice.live=true;h.draw();h.pick('Explain approval');h.pick('Explain recovery');assert.deepEqual(h.voice.spoken,['Explain approval']);h.input().props.onChange({target:{value:'another question'}});h.draw();h.submit();assert.deepEqual(h.voice.spoken,['Explain approval']);assert.equal(h.input().props.value,'another question')});
test('suggestions cannot interrupt an externally started spoken or pending answer',()=>{for(const phase of ['speaking','transcribing']){const h=harness({judgeSuggestions:true});h.voice.phase=phase;h.draw();h.pick('Explain approval');assert.deepEqual(h.voice.spoken,[]);assert.equal(h.voice.starts,0)}});

test('completed request releases the next suggestion without replaying any queued click',()=>{const h=harness({judgeSuggestions:true});h.voice.live=true;h.draw();h.pick('First');h.voice.phase='transcribing';h.draw();h.pick('Ignored');h.voice.reply='Complete answer';h.voice.phase='listening';h.draw();h.draw();h.pick('Next');assert.deepEqual(h.voice.spoken,['First','Next'])});
test('connection failure releases a pending suggestion for an explicit retry',()=>{const h=harness({judgeSuggestions:true});h.pick('First');h.voice.notice='Connection failed';h.draw();h.draw();h.pick('Retry');assert.equal(h.voice.starts,2);assert.deepEqual(h.voice.spoken,[])});
