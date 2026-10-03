// Reuse the local Codex login without exporting or duplicating refresh tokens.
import {readFile, realpath} from 'node:fs/promises';
import {join} from 'node:path';
import {homedir} from 'node:os';
import {createHash} from 'node:crypto';
import {execFile, spawn} from 'node:child_process';
import {promisify} from 'node:util';
import {createInterface} from 'node:readline';
import {createRequire, findPackageJSON} from 'node:module';
import {pathToFileURL} from 'node:url';

const execFileAsync = promisify(execFile);
export const reuseCliAuth = () => !['0','false','off'].includes((process.env.HUNTUN_PI_REUSE_CLI_AUTH || '').toLowerCase());
const codexHome = () => process.env.CODEX_HOME || join(homedir(), '.codex');

export async function readCodexAccess() {
  const home = codexHome();
  // Respect the selected store; a leftover file must not override keyring login/logout.
  let mode = 'file';
  try {
    const config = (await readFile(join(home, 'config.toml'), 'utf8')).split(/^\s*\[/m)[0];
    mode = config.match(/^\s*cli_auth_credentials_store\s*=\s*["'](file|keyring|auto|ephemeral)["']/m)?.[1] || mode;
  } catch {}
  if (mode === 'ephemeral') return undefined;
  let data;
  if (process.platform === 'darwin' && ['keyring','auto'].includes(mode)) {
    try {
      const canonical = await realpath(home).catch(() => home);
      const account = 'cli|' + createHash('sha256').update(canonical).digest('hex').slice(0,16);
      const {stdout} = await execFileAsync('/usr/bin/security', ['find-generic-password','-s','Codex Auth','-a',account,'-w'], {timeout:2000, maxBuffer:1048576});
      data = JSON.parse(stdout);
    } catch {}
  }
  if (!data && mode !== 'keyring') {
    try { data = JSON.parse(await readFile(join(home, 'auth.json'), 'utf8')); } catch {}
  }
  if (!data?.tokens?.access_token || (data.auth_mode && !['chatgpt','chatgptAuthTokens'].includes(data.auth_mode))) return undefined;
  try {
    const access = data.tokens.access_token;
    const claims = JSON.parse(Buffer.from(access.split('.')[1], 'base64url').toString());
    if (!claims['https://api.openai.com/auth']?.chatgpt_account_id || !Number.isFinite(claims.exp)) return undefined;
    // Only access + expiry leave this function. The original store keeps refresh ownership.
    return {access, expires:claims.exp * 1000};
  } catch { return undefined; }
}

async function refreshWithCodex(signal) {
  // The official account/read RPC rotates and persists tokens in Codex's own store.
  // Do not surface RPC bodies, CLI diagnostics or credential values to Pi's JSONL.
  const child = spawn(process.env.HUNTUN_CODEX_BIN || 'codex', ['app-server'], {
    cwd:codexHome(), stdio:['pipe','pipe','ignore'], env:process.env,
  });
  const lines = createInterface({input:child.stdout});
  const pending = new Map();
  const fail = () => {
    for (const reject of pending.values()) reject(new Error('Codex login refresh failed; run codex login.'));
    pending.clear();
  };
  child.on('error', fail);
  child.on('exit', fail);
  child.stdin.on('error', fail);
  const timer = setTimeout(() => { fail(); child.kill(); }, 20000);
  const abort = () => { fail(); child.kill(); };
  signal?.addEventListener('abort', abort, {once:true});
  const responses = new Map();
  lines.on('line', line => {
    try {
      const event = JSON.parse(line);
      const resolve = responses.get(event.id);
      if (!resolve) return;
      responses.delete(event.id);
      pending.delete(event.id);
      if (event.error) resolve(false); else resolve(true);
    } catch {}
  });
  const request = (id,method,params) => new Promise((resolve,reject) => {
    signal?.throwIfAborted();
    pending.set(id,reject);
    responses.set(id,resolve);
    child.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
  try {
    if (!await request(1,'initialize',{clientInfo:{name:'huntun_pi_auth',version:'1.0.0'}})) throw new Error('Codex login refresh initialization failed.');
    child.stdin.write(JSON.stringify({method:'initialized'})+'\n');
    if (!await request(2,'account/read',{refreshToken:true})) throw new Error('Codex login refresh failed; run codex login.');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    lines.close();
    child.stdin.end();
    child.kill();
  }
}

export async function codexAccess(signal, lockfile) {
  let credential = await readCodexAccess();
  if (!credential) throw new Error('No reusable Codex ChatGPT login. Run codex login.');
  if (credential.expires > Date.now()+300000) return credential.access;
  // Coordinate Huntun workers; double-check after acquiring the lock. Native
  // Codex still owns refresh and persistence, including its own race handling.
  const release = await lockfile.lock(codexHome(), {
    realpath:false, lockfilePath:join(codexHome(), '.huntun-pi-refresh.lock'),
    stale:30000, retries:{retries:40,factor:1,minTimeout:250,maxTimeout:250},
  });
  try {
    signal?.throwIfAborted();
    credential = await readCodexAccess();
    if (!credential) throw new Error('Codex was logged out; run codex login.');
    if (credential.expires <= Date.now()+300000) {
      await refreshWithCodex(signal);
      credential = await readCodexAccess();
    }
    if (!credential || credential.expires <= Date.now()+30000) throw new Error('Codex login expired; run codex login.');
    return credential.access;
  } finally { await release(); }
}

export async function codexLoginProvider(sdkEntry) {
  const require = createRequire(sdkEntry);
  const aiPackage = findPackageJSON('@earendil-works/pi-ai', sdkEntry);
  const {openaiCodexProvider} = await import(new URL('./dist/providers/openai-codex.js', pathToFileURL(aiPackage)).href);
  const lockfile = require('proper-lockfile');
  const provider = openaiCodexProvider();
  // Keep Pi's own stored OAuth login working. Ambient auth is consulted only
  // when Pi has no credential for this provider; no auth.json mutation occurs.
  provider.auth.apiKey = {
    check:async ({credential}) => credential ? {type:'api_key',source:'Pi credentials'} :
      await readCodexAccess() ? {type:'oauth',source:'Codex CLI login'} : undefined,
    resolve:async ({signal,credential}) => ({auth:{apiKey:credential ? credential.key : await codexAccess(signal,lockfile)},
      source:credential ? 'Pi credentials' : 'Codex CLI login'}),
  };
  return provider;
}

export async function closeCodexConnections(sdkEntry) {
  const aiPackage = findPackageJSON('@earendil-works/pi-ai', sdkEntry);
  const {closeOpenAICodexWebSocketSessions} = await import(new URL('./dist/api/openai-codex-responses.js', pathToFileURL(aiPackage)).href);
  closeOpenAICodexWebSocketSessions();
}
