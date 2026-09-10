import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/chat-history.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports});
const {emptyHistory,addMessage,saveReply,readHistory,historyKey}=exports;
test('reload restores both user messages and actual replies without an executable action',()=>{
 let h=addMessage(emptyHistory(),'Show milk','m1','c1');h=saveReply(h,'c1','m1','Here are the milk options.');
 const restored=readHistory(JSON.stringify(h));assert.equal(restored.chats[0].messages[0].reply,'Here are the milk options.');assert.equal(restored.activeId,'c1');assert.equal('proposal' in restored.chats[0].messages[0],false);
});
test('new conversation preserves and reopens earlier conversations',()=>{
 let h=addMessage(emptyHistory(),'Milk','m1','c1');h=addMessage({...h,activeId:null},'Bread','m2','c2');
 assert.equal(h.chats.length,2);assert.equal(h.activeId,'c2');h=readHistory(JSON.stringify({...h,activeId:'c1'}));assert.equal(h.chats.find(c=>c.id===h.activeId).messages[0].user,'Milk');
});
test('late answer targets original message, never another conversation',()=>{
 let h=addMessage(emptyHistory(),'Milk','m1','c1');h=addMessage({...h,activeId:null},'Bread','m2','c2');h=saveReply(h,'c1','m1','Milk reply');assert.equal(h.chats[1].messages[0].reply,null);assert.equal(h.chats[0].messages[0].reply,'Milk reply');
});
test('session namespaces do not leak history between buyers',()=>{assert.notEqual(historyKey('session:a/razorai/shopping'),historyKey('session:b/razorai/shopping'));assert.throws(()=>historyKey(''));});
test('malformed saved data cannot break shopping',()=>{for(const raw of ['bad','null','{}','{"chats":[null,{}]}'])assert.equal(readHistory(raw).chats.length,0);});

test('bootstrap retry merges by conversation ID without duplicate sidebar rows',()=>{
 const h=addMessage(emptyHistory(),'Milk','m1','c1');const merged=exports.mergeHistory(h,h);assert.equal(merged.chats.length,1);assert.equal(readHistory(JSON.stringify({...h,chats:[h.chats[0],h.chats[0]]})).chats.length,1);
});

test('persisting an unchanged reply preserves identity and cannot feed a render loop',()=>{
 const initial=addMessage(emptyHistory(),'Milk','m1','c1');
 const saved=saveReply(initial,'c1','m1','Added milk');assert.notEqual(saved,initial);
 for(let i=0;i<100;i++)assert.equal(saveReply(saved,'c1','m1','Added milk'),saved);
 assert.equal(saveReply(saved,'missing','m1','Ignored'),saved);
 assert.equal(saveReply(saved,'c1','missing','Ignored'),saved);
 const updated=saveReply(saved,'c1','m1','Added two milk');assert.notEqual(updated,saved);
 assert.equal(updated.chats[0].messages[0].reply,'Added two milk');
});
