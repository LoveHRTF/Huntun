// Authentication/refresh fixtures only: no real tokens or paid model calls.
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, readFile, rm, chmod} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createRequire} from 'node:module';
import {codexAccess, codexLoginProvider, readCodexAccess} from '../../huntun/pi_auth.js';
import {claudeArgs} from '../../huntun/pi_claude_cli.js';

const exec = promisify(execFile);
const sdk = process.argv[2];
const {ModelRuntime} = await import(sdk);
const lockfile = createRequire(sdk)('proper-lockfile');
const root = await mkdtemp(join(tmpdir(),'huntun-pi-auth-fixture-'));
const codex = join(root,'codex'), pi = join(root,'pi');
await mkdir(codex); await mkdir(pi);
process.env.CODEX_HOME = codex;
process.env.PI_CODING_AGENT_DIR = pi;
process.env.PI_OFFLINE = '1';
const jwt = (tag,expires=Date.now()+3600000) =>
  'fixture.' + Buffer.from(JSON.stringify({exp:Math.floor(expires/1000),
    'https://api.openai.com/auth':{chatgpt_account_id:'fixture-account'},tag})).toString('base64url')+'.signature';
const auth = (access) => ({auth_mode:'chatgpt',tokens:{access_token:access,refresh_token:'original-private-refresh',id_token:'fixture-id',account_id:'fixture-account'},last_refresh:'fixture',unrelated:'preserve'});
const authPath = join(codex,'auth.json');
const writeAuth = async (access) => writeFile(authPath,JSON.stringify(auth(access)),{mode:0o600});
const runtime = async () => {
  const r = await ModelRuntime.create({allowModelNetwork:false});
  r.registerNativeProvider(await codexLoginProvider(sdk));
  await r.getAvailable();
  return r;
};
try {
  assert.equal(await readCodexAccess(),undefined);
  assert.equal((await (await runtime()).getAvailable('openai-codex')).length,0);
  const access = jwt('first');
  await writeAuth(access);
  const before = await readFile(authPath,'utf8');
  let r = await runtime();
  assert.ok((await r.getAvailable('openai-codex')).length>0);
  assert.equal(r.isUsingSubscription('openai-codex'),true);
  assert.equal((await r.getAuth('openai-codex')).auth.apiKey,access);
  assert.equal(await readFile(authPath,'utf8'),before);
  const piAuth = JSON.parse(await readFile(join(pi,'auth.json'),'utf8').catch(()=> '{}'));
  assert.deepEqual(piAuth,{}); // no import/copy of subscription credentials
  const rotated = jwt('native-cli-rotation');
  await writeAuth(rotated);
  assert.equal((await r.getAuth('openai-codex')).auth.apiKey,rotated);
  await rm(authPath);
  await assert.rejects(r.getAuth('openai-codex'),e => e.message.includes('API key auth failed'));

  // An explicitly stored Pi login owns auth; local Codex must not switch accounts.
  const own = jwt('pi-own');
  await writeFile(join(pi,'auth.json'),JSON.stringify({'openai-codex':{type:'oauth',access:own,refresh:'pi-own-refresh',expires:Date.now()+3600000}}));
  await writeAuth(access);
  r = await runtime();
  assert.equal((await r.getAuth('openai-codex')).auth.apiKey,own);
  await writeFile(join(pi,'auth.json'),'{}');
  await writeFile(join(codex,'config.toml'),'cli_auth_credentials_store="ephemeral"\n');
  assert.equal(await readCodexAccess(),undefined);
  // Parsing only the root table prevents a provider's option from selecting a store.
  await writeFile(join(codex,'config.toml'),'[model_providers.fixture]\ncli_auth_credentials_store="ephemeral"\n');
  assert.equal((await readCodexAccess()).access,access);
  await rm(join(codex,'config.toml'));
  await writeFile(authPath,JSON.stringify({auth_mode:'apikey',OPENAI_API_KEY:'fixture-api-key',tokens:auth(access).tokens}));
  assert.equal(await readCodexAccess(),undefined);
  await writeFile(authPath,'invalid-json');
  assert.equal(await readCodexAccess(),undefined);

  // Two independent workers share an expired login. Only the canonical CLI
  // refreshes, exactly once, and retains unrelated native auth fields.
  const fresh = jwt('refreshed');
  await writeAuth(jwt('expired',Date.now()-10000));
  const cli = join(root,'fake-codex');
  await writeFile(cli,`#!${process.execPath}
const fs=require('node:fs'),path=require('node:path'),rl=require('node:readline');
rl.createInterface({input:process.stdin}).on('line',async line=>{
 const e=JSON.parse(line);
 if(e.method==='initialize') console.log(JSON.stringify({id:e.id,result:{}}));
 if(e.method==='account/read') {
  fs.appendFileSync(path.join(process.env.CODEX_HOME,'refresh.log'),JSON.stringify(e)+'\\n');
  await new Promise(r=>setTimeout(r,500));
  const p=path.join(process.env.CODEX_HOME,'auth.json'),a=JSON.parse(fs.readFileSync(p));
  a.tokens.access_token=${JSON.stringify(fresh)};a.tokens.refresh_token='rotated-private-refresh';
  fs.writeFileSync(p,JSON.stringify(a));
  console.log(JSON.stringify({id:e.id,result:{account:{type:'chatgpt'}}}));
 }
});
`);
  await chmod(cli,0o700);
  process.env.HUNTUN_CODEX_BIN = cli;
  const helper = new URL('../../huntun/pi_auth.js',import.meta.url).href;
  const worker = `import {createRequire} from 'node:module';import {codexAccess} from ${JSON.stringify(helper)};
const key=await codexAccess(AbortSignal.timeout(10000),createRequire(${JSON.stringify(sdk)})('proper-lockfile'));
console.log(key===${JSON.stringify(fresh)} ? 'OK' : 'FAIL');`;
  const results = await Promise.all([1,2].map(()=>exec(process.execPath,['--input-type=module','-e',worker],{env:process.env,timeout:15000})));
  assert.ok(results.every(x=>x.stdout.trim()==='OK' && !x.stderr));
  const calls = (await readFile(join(codex,'refresh.log'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(calls.length,1);
  assert.equal(calls[0].params.refreshToken,true);
  const canonical = JSON.parse(await readFile(authPath,'utf8'));
  assert.equal(canonical.unrelated,'preserve');
  assert.equal(canonical.tokens.refresh_token,'rotated-private-refresh');
  assert.deepEqual(JSON.parse(await readFile(join(pi,'auth.json'),'utf8')),{});

  // Native errors are sanitized; fixture secrets/RPC response bodies cannot leak.
  await writeAuth(jwt('expired-again',Date.now()-10000));
  await writeFile(cli,`#!${process.execPath}
require('node:readline').createInterface({input:process.stdin}).on('line',line=>{
 const e=JSON.parse(line);if(e.id)console.log(JSON.stringify({id:e.id,error:{message:'original-private-refresh SECRET'}}));
});`);
  await assert.rejects(codexAccess(AbortSignal.timeout(5000),lockfile),e => !e.message.includes('SECRET') && !e.message.includes('original-private-refresh'));

  // CLI launcher preserves auth flags and adds per-process plugin settings only.
  const args=['--print','--settings',JSON.stringify({disableAllHooks:true,enabledPlugins:{'custom@fixture':false}}),'--permission-mode','dontAsk'];
  const patched=claudeArgs(args);
  assert.equal(JSON.parse(patched[2]).enabledPlugins['plugin-authoring@builtin'],false);
  assert.equal(JSON.parse(patched[2]).enabledPlugins['custom@fixture'],false);
  assert.equal(JSON.parse(patched[2]).disableAllHooks,true);
  assert.deepEqual(patched.slice(3),args.slice(3));
  assert.deepEqual(claudeArgs(['auth','status']),['auth','status']);
  assert.ok(!args[2].includes('plugin-authoring')); // no mutation of caller config
  console.log('Pi CLI authentication checks passed: discovery, precedence, logout/rotation, concurrent canonical refresh, redaction and child-only Claude settings.');
} finally {await rm(root,{recursive:true,force:true});}
