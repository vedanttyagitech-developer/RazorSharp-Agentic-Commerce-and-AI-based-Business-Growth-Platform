import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';

test('custom voice and API ports stay aligned across ticket, websocket and origins',()=>{
 const root=mkdtempSync(join(tmpdir(),'voice-launch-'));
 try {
  mkdirSync(join(root,'scripts'));mkdirSync(join(root,'dist'));
  writeFileSync(join(root,'dist/index.html'),'demo');
  writeFileSync(join(root,'scripts/run_mounted_demo.sh'),readFileSync(new URL('../../../scripts/run_mounted_demo.sh',import.meta.url)));
  writeFileSync(join(root,'scripts/run_demo.sh'),'printf "%s\\n" "$VOICE_GATEWAY_PORT" "$VOICE_GATEWAY_URL" "$VOICE_PUBLIC_ORIGIN" "$VOICE_GATEWAY_ALLOWED_ORIGINS"');
  const result=spawnSync('/bin/bash',[join(root,'scripts/run_mounted_demo.sh')],{encoding:'utf8',env:{PATH:process.env.PATH,FRONTEND_DIST:join(root,'dist'),PORT:'9000',VOICE_GATEWAY_PORT:'8200'}});
  assert.equal(result.status,0,result.stderr);
  const lines=result.stdout.trim().split('\n');
  assert.deepEqual(lines.slice(0,3),['8200','http://127.0.0.1:8200','http://127.0.0.1:8200']);
  assert.ok(lines[3].includes('http://localhost:9000'));
 } finally {rmSync(root,{recursive:true,force:true});}
});
