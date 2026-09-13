import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

test('executed merchant change reloads catalogue and ignores an obsolete response',async()=>{
 const window=new EventTarget();
 const state=[],effects=[],pending=[];let slot=0,effectSlot=0,result;
 const react={useState(initial){const i=slot++;if(!(i in state))state[i]=initial;return [state[i],value=>{state[i]=typeof value==='function'?value(state[i]):value;}];},useEffect(fn,deps){const i=effectSlot++;const old=effects[i];if(!old||deps.some((d,j)=>d!==old.deps[j])){old?.cleanup?.();effects[i]={deps,cleanup:fn()};}}};
 const loadedModule=(path,require)=>{const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL(path,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,require,window,Event,AbortController});return exports;};
 const sync=loadedModule('../lib/merchant-sync.ts',()=>({}));
 const catalogue=loadedModule('../lib/catalogue.ts',name=>name==='react'?react:name==='./merchant-sync'?sync:{CommerceError:Error,commerce:{catalogue:{list:()=>new Promise(resolve=>pending.push(resolve))}}});
 const render=()=>{slot=0;effectSlot=0;result=catalogue.useCatalogue();};
 const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));render();};
 const page=stock=>({products:[{sku:'milk',display_name:'Milk',category:'dairy',unit_label:'packet',unit_price_minor:2800,stock_units:stock,is_listed:false,is_available:false}],next_cursor:null});
 render();assert.equal(pending.length,1);
 sync.merchantStateChanged();render();assert.equal(pending.length,2);
 pending[1](page(445));await flush();assert.equal(result.products[0].stock,445);assert.equal(result.products[0].isListed,false);assert.equal(result.products[0].isAvailable,false);assert.equal(result.loading,false);
 pending[0](page(444));await flush();assert.equal(result.products[0].stock,445);
 for(const effect of effects)effect.cleanup?.();
 sync.merchantStateChanged();render();assert.equal(pending.length,2);
});

test('a merchant write notifies another tab, ignores other storage keys and unsubscribes',()=>{
 const merchant=new EventTarget(),buyer=new EventTarget();let refreshed=0;
 merchant.localStorage={setItem(key){const event=new Event('storage');event.key=key;buyer.dispatchEvent(event);}};
 function load(window){const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/merchant-sync.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,window,Event});return exports;}
 const sender=load(merchant),reader=load(buyer);
 const stop=reader.subscribeMerchantChanges(()=>refreshed++);
 sender.merchantStateChanged();assert.equal(refreshed,1);
 merchant.localStorage.setItem('unrelated');assert.equal(refreshed,1);
 buyer.dispatchEvent(new Event('focus'));assert.equal(refreshed,2);
 stop();sender.merchantStateChanged();assert.equal(refreshed,2);
});
